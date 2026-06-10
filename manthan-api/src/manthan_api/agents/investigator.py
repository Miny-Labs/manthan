"""manthan-investigator - the investigation agent as its own A2A service.

Run it:  uvicorn manthan_api.agents.investigator:app --host 0.0.0.0 --port 8080

Surface:
  GET  /.well-known/agent-card.json  investigator-flavored AgentCard
                                     (identity agent_id 'manthan-investigator')
  POST /a2a                          JSON-RPC: investigate_dispute starts a
                                     case + runs the ADK agent IN-PROCESS as a
                                     background task; tasks/get + the 6 read
                                     skills delegate to the shared dispatcher
                                     over PgCaseStore.

This REPLACES the retired workers/investigate.py NOTIFY-mirror pipeline. The
projections are identical (same services.case_store functions the worker's
mirror was extracted into) - there's just no NOTIFY hop anymore: the agent
that accepted the A2A task is the agent that writes the events.

Event mapping (one ADK run -> Postgres):
  case_opened / agent_thought / tool_call / tool_result  -> append_event
  finding_recorded   -> append_event + record_finding_projection
  brief_drafted      -> append_event + record_brief
  hitl_pause         -> append_event (escalates at finalize)
  case_closed        -> append_event, then finalize_case (policy auto-approval
                        + status transition) once the run drains

Dedupe guards (mirroring the old worker + webhook):
  * an `investigation_started` event is appended before the run; if one
    already exists for the thread the run is skipped.
  * triggers carrying structured.event_id (Stripe webhooks routed through
    triage) dedupe against cases.trigger_payload->>'event_id'.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from uuid import UUID

from fastapi import FastAPI, Request
from pydantic import ValidationError

from manthan_agent import config as agent_config
from manthan_agent.a2a.card import build_agent_card
from manthan_agent.a2a.server import dispatch
from manthan_agent.coral_session import (
    clear_active_coral_session,
    coral_mcp_session,
    set_active_coral_session,
)
from manthan_agent.loop import run_case
from manthan_agent.types import CaseTrigger

from manthan_api.config import get_settings
from manthan_api.db import close_pool, get_pool, init_pool
from manthan_api.services import case_store
from manthan_api.services.a2a_store import (
    PgCaseStore,
    _default_org_slug,
    insert_case_from_trigger,
)
from manthan_api.agents.common import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_PARSE_ERROR,
    agent_identity,
    extract_skill,
    parse_jsonrpc,
    public_base_url,
    rpc_err,
    rpc_ok,
)

logger = logging.getLogger("agents.investigator")

AGENT_ID = "manthan-investigator"

# Strong refs to in-flight investigations so asyncio doesn't GC them.
_BG_TASKS: set[asyncio.Task[None]] = set()

# Lazily resolved orgs (cached by slug) + store. A2A traffic defaults to the
# MANTHAN_A2A_ORG slug; triage's per-org webhook path can name another.
_org_ids: dict[str, UUID] = {}
_store: Any = None


def get_store() -> Any:
    global _store
    if _store is None:
        _store = PgCaseStore()
    return _store


async def _resolve_org_id(org_slug: str | None = None) -> UUID:
    slug = org_slug or _default_org_slug()
    if slug not in _org_ids:
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow("SELECT id FROM orgs WHERE slug = $1", slug)
        if row is None:
            raise RuntimeError(
                f"A2A org not found: {slug!r} - set MANTHAN_A2A_ORG or run "
                "scripts/bootstrap_dev_org.py"
            )
        _org_ids[slug] = row["id"]
    return _org_ids[slug]


async def _existing_case_for_event(org_id: UUID, event_id: str) -> UUID | None:
    """Stripe-event idempotency: one case per structured.event_id."""
    async with get_pool().acquire() as conn:
        return await conn.fetchval(
            """
            SELECT id FROM cases
            WHERE org_id = $1 AND trigger_payload->>'event_id' = $2
            LIMIT 1
            """,
            org_id,
            event_id,
        )


# ──────────────────────────────────────────────────────────────────────
# The in-process investigation runner
# ──────────────────────────────────────────────────────────────────────


def _spawn_investigation(org_id: UUID, case_id: UUID, trigger: dict[str, Any]) -> None:
    """Fire-and-forget the run on the service's event loop."""
    task = asyncio.get_running_loop().create_task(
        _run_investigation(org_id, case_id, trigger)
    )
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


def _case_trigger(thread_id: UUID, trigger: dict[str, Any]) -> CaseTrigger:
    """Build the agent's CaseTrigger; tolerate unknown source surfaces.

    case_id = thread_id, matching the retired worker (the agent's internal
    case label was always the thread id)."""
    kwargs: dict[str, Any] = {
        "case_id": str(thread_id),
        "text": trigger.get("text") or "",
        "structured": trigger.get("structured") or {},
        "source_surface": trigger.get("source_surface") or "api",
    }
    try:
        return CaseTrigger(**kwargs)
    except ValidationError:
        kwargs["source_surface"] = "api"
        return CaseTrigger(**kwargs)


async def _run_investigation(
    org_id: UUID, case_id: UUID, trigger: dict[str, Any]
) -> None:
    """Drive run_case() and write every yielded Event straight to PG.

    Same projections as the retired worker's _mirror_event/_finalize_case,
    via the services.case_store functions they were extracted into."""
    log = logger.getChild(str(case_id)[:8])

    async with get_pool().acquire() as conn:
        thread_id = await conn.fetchval(
            "SELECT thread_id FROM cases WHERE id = $1", case_id
        )
    if thread_id is None:
        log.warning("no case row for %s - skipping", case_id)
        return

    # Dedupe: if investigation_started already exists, another runner
    # (or a duplicate A2A delivery) is already on it.
    async with get_pool().acquire() as conn:
        already = await conn.fetchval(
            """
            SELECT 1 FROM events
            WHERE org_id = $1 AND thread_id = $2 AND type = 'investigation_started'
            LIMIT 1
            """,
            org_id,
            thread_id,
        )
    if already:
        log.info("already in flight (investigation_started exists) - skipping")
        return

    await case_store.append_event(
        org_id, thread_id, "investigation_started", "system",
        {
            "trigger_surface": trigger.get("source_surface") or "api",
            "runner": AGENT_ID,
        },
    )

    cfg = agent_config.load()
    coral_binary = get_settings().coral_binary
    ct = _case_trigger(thread_id, trigger)

    has_brief = False
    hitl = False
    errored = False

    log.info("investigation start org=%s thread=%s", org_id, thread_id)
    try:
        async with coral_mcp_session(coral_binary) as session:
            token = set_active_coral_session(session)
            try:
                async for evt in run_case(ct, cfg):
                    data = case_store.serialize(evt.data)
                    if not isinstance(data, dict):
                        data = {"value": data}

                    await case_store.append_event(
                        org_id, thread_id, evt.kind, evt.actor, data,
                        trace_id=evt.trace_id, span_id=evt.span_id,
                    )

                    if evt.kind == "finding_recorded":
                        await case_store.record_finding_projection(
                            org_id, case_id, data
                        )
                    elif evt.kind == "brief_drafted":
                        await case_store.record_brief(
                            org_id, thread_id, case_id, data
                        )
                        has_brief = True
                    elif evt.kind == "hitl_pause":
                        hitl = True
                    elif evt.kind == "case_closed":
                        if data.get("reason") == "error":
                            errored = True
            finally:
                clear_active_coral_session(token)
    except asyncio.CancelledError:
        # Cloud Run shutdown / deploy cancels in-flight background tasks.
        # CancelledError is a BaseException in py3.12 — without this branch
        # the case would stay 'investigating' forever (zombie). Best-effort
        # mark + re-raise so cancellation semantics are preserved.
        log.warning("investigation cancelled (shutdown) - marking errored")
        try:
            await case_store.append_event(
                org_id, thread_id, "error", "system",
                {"reason": "investigator_cancelled",
                 "detail": "service shutdown during investigation - retrigger the case"},
            )
            await case_store.update_status(case_id, "errored")
        except Exception:  # noqa: BLE001 - pool may already be closing
            log.warning("could not record cancellation for case %s", case_id)
        raise
    except Exception as exc:  # noqa: BLE001 - background task must not raise
        log.exception("investigation crashed: %s", exc)
        try:
            await case_store.append_event(
                org_id, thread_id, "error", "system",
                {
                    "reason": "investigator_exception",
                    "detail": f"{type(exc).__name__}: {exc}",
                },
            )
            await case_store.update_status(case_id, "errored")
        except Exception:  # noqa: BLE001
            log.exception("could not record crash for case %s", case_id)
        return

    try:
        final_status = await case_store.finalize_case(
            org_id, thread_id, case_id,
            has_brief=has_brief, hitl=hitl, errored=errored,
        )
        log.info("investigation done - final status %s", final_status)
    except Exception:  # noqa: BLE001
        log.exception("finalize failed for case %s", case_id)


# ──────────────────────────────────────────────────────────────────────
# The A2A entrypoint (also called in-process by triage's local fallback)
# ──────────────────────────────────────────────────────────────────────


async def handle_investigate(
    trigger: dict[str, Any], *, actor: str = "a2a:remote"
) -> dict[str, Any]:
    """Create the case, then run the investigation as a background task.

    This is the investigate_dispute skill body. It is also importable and
    directly awaitable - manthan_api.agents.triage calls it in-process when
    INVESTIGATOR_A2A_URL is unset (local dev mode, no second service)."""
    await init_pool()  # idempotent; needed on the triage-embedded path
    # Honor an explicit org from the caller (triage's per-org webhook path);
    # fall back to the single configured A2A org.
    org_id = await _resolve_org_id(
        trigger.get("org_slug") if isinstance(trigger, dict) else None
    )

    structured = trigger.get("structured") or {}
    event_id = structured.get("event_id")
    if event_id:
        existing = await _existing_case_for_event(org_id, str(event_id))
        if existing is not None:
            return {
                "id": str(existing),
                "case_id": str(existing),
                "status": {"state": "submitted"},
                "kind": "task",
                "deduplicated": True,
            }

    extra: dict[str, Any] = {}
    if event_id:
        extra["stripe_event_id"] = event_id
    if structured.get("event_type"):
        extra["stripe_event_type"] = structured["event_type"]

    # Triage suggests deterministic short ids for webhook events (DSP-…,
    # EFW-…, INV-…) via short_id_hint - same convention the old direct
    # webhook path used.
    hint = trigger.get("short_id_hint")
    short_id_hint = hint if isinstance(hint, str) and hint else None

    case_id, short_id = await insert_case_from_trigger(
        org_id, trigger, actor=actor,
        short_id=short_id_hint, extra_event_data=extra or None,
    )
    _spawn_investigation(org_id, case_id, trigger)
    return {
        "id": str(case_id),
        "case_id": str(case_id),
        "short_id": short_id,
        "status": {"state": "submitted"},
        "kind": "task",
    }


# ──────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    await init_pool()
    logger.info("%s startup complete", AGENT_ID)
    try:
        yield
    finally:
        for task in list(_BG_TASKS):
            task.cancel()
        await asyncio.gather(*_BG_TASKS, return_exceptions=True)
        await close_pool()


app = FastAPI(title="Manthan Investigator (A2A)", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "agent": AGENT_ID, "in_flight": len(_BG_TASKS)}


@app.get("/.well-known/agent-card.json")
async def agent_card(request: Request) -> dict[str, Any]:
    identity = agent_identity(
        AGENT_ID, os.environ.get("MANTHAN_MODEL") or "gemini-3.1-pro-preview"
    )
    return build_agent_card(public_base_url(request), identity=identity)


@app.post("/a2a")
async def a2a_rpc(request: Request) -> dict[str, Any]:
    payload = await parse_jsonrpc(request)
    if payload is None:
        return rpc_err(None, JSONRPC_PARSE_ERROR, "invalid JSON")

    # investigate_dispute is intercepted so the case actually RUNS here
    # (the generic dispatcher's create_investigation only inserts the row).
    if payload.get("method") == "message/send":
        skill, args = extract_skill(payload.get("params", {}) or {})
        if skill == "investigate_dispute":
            try:
                result = await handle_investigate(args, actor="a2a:remote")
            except Exception as exc:  # noqa: BLE001
                return rpc_err(
                    payload.get("id"), JSONRPC_INTERNAL_ERROR,
                    f"{type(exc).__name__}: {exc}",
                )
            return rpc_ok(payload.get("id"), result)

    # tasks/get + the 6 read skills ride the shared dispatcher.
    return await dispatch(payload, get_store())
