"""manthan-advisor - the conversational A2A face of Manthan.

Run it:  uvicorn manthan_api.agents.advisor:app --host 0.0.0.0 --port 8080

Surface:
  GET  /.well-known/agent-card.json   advisor-flavored AgentCard
                                      (identity agent_id 'manthan-advisor')
  POST /a2a                           JSON-RPC over the EXTENDED PgCaseStore:

    advisor skills (this module):
      ask                  NL question over a case -> cited answer (ONE Gemini
                           call grounded on the case's findings + brief from
                           PG; cites finding indices; degrades to an explicit
                           'LLM unavailable' answer without GOOGLE_API_KEY)
      precheck_refund      CS/refund-desk agent asks before granting a refund:
                           prior disputes, outcomes, open cases + a
                           deterministic recommendation
                           (investigate_first | low_risk | high_risk)
      get_customer_history episodic memory by customer_ref (cases + decisions)
      dispute_exposure     aggregates for CFO agents: open case count + sum of
                           decision_amount_minor by status
      contribute_evidence  external agent pushes evidence onto a case thread
                           (evidence_contributed event, actor a2a:<contributor>)

    plus the 6 read skills + tasks/get, delegated to the shared
    manthan_agent.a2a.server.dispatch over the same store.

`dispatch_advisor()` is importable - the main API's /a2a gateway routes
through it so both surfaces serve the identical skill set.
"""

from __future__ import annotations

import inspect
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request

from manthan_agent.a2a.card import QUERY_SKILL_IDS, build_agent_card
from manthan_agent.a2a.server import dispatch

from manthan_api.db import close_pool, init_pool
from manthan_api.services.a2a_store import PgCaseStore
from manthan_api.agents.common import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_PARSE_ERROR,
    agent_identity,
    data_message,
    extract_skill,
    parse_jsonrpc,
    public_base_url,
    rpc_err,
    rpc_ok,
)

logger = logging.getLogger("agents.advisor")

AGENT_ID = "manthan-advisor"

ADVISOR_SKILLS: list[dict[str, Any]] = [
    {
        "id": "ask",
        "name": "Ask about a case",
        "description": (
            "Natural-language question over a case; answers are grounded on "
            "the case's recorded findings + brief and cite finding indices."
        ),
        "tags": ["advise", "qa", "citations"],
        "examples": ['ask {"case_id": "…", "question": "why fight and not refund?"}'],
    },
    {
        "id": "precheck_refund",
        "name": "Pre-refund risk check",
        "description": (
            "Before granting a refund, ask Manthan: prior disputes, decision "
            "outcomes, open cases and a deterministic recommendation "
            "(investigate_first | low_risk | high_risk) for this customer."
        ),
        "tags": ["advise", "refunds", "risk", "friendly-fraud"],
        "examples": ['precheck_refund {"customer_ref": "maya@example.com", "amount_minor": 56000}'],
    },
    {
        "id": "get_customer_history",
        "name": "Get customer history",
        "description": "Episodic memory by customer_ref: their cases with decisions.",
        "tags": ["state", "read", "memory", "customer"],
        "examples": ['get_customer_history {"customer_ref": "maya@example.com"}'],
    },
    {
        "id": "dispute_exposure",
        "name": "Get dispute exposure",
        "description": (
            "Aggregate exposure across the book: open case count + the sum of "
            "decision_amount_minor grouped by status. Built for CFO agents."
        ),
        "tags": ["state", "read", "aggregate", "finance"],
        "examples": ["dispute_exposure {}"],
    },
    {
        "id": "contribute_evidence",
        "name": "Contribute evidence to a case",
        "description": (
            "Push evidence into an open case (e.g. a CS chat transcript). "
            "Recorded as an evidence_contributed event with the caller's "
            "agent identity as actor."
        ),
        "tags": ["collaborate", "write", "evidence", "provenance"],
        "examples": [
            'contribute_evidence {"case_id": "…", "contributor": "refund-desk", '
            '"evidence": {"transcript": "…"}}'
        ],
    },
]

ADVISOR_SKILL_IDS = {s["id"] for s in ADVISOR_SKILLS}


# ──────────────────────────────────────────────────────────────────────
# Dispatch
# ──────────────────────────────────────────────────────────────────────


async def _run_advisor_skill(skill: str, args: dict[str, Any], store: Any) -> Any:
    if skill == "ask":
        return await store.ask(
            str(args.get("question") or ""), case_id=args.get("case_id")
        )
    if skill == "precheck_refund":
        amount = args.get("amount_minor")
        return await store.precheck_refund(
            str(args.get("customer_ref") or ""),
            amount_minor=int(amount) if isinstance(amount, (int, float)) else None,
        )
    if skill == "get_customer_history":
        return await store.get_customer_history(
            str(args.get("customer_ref") or ""), int(args.get("limit", 20))
        )
    if skill == "dispute_exposure":
        return await store.dispute_exposure()
    if skill == "contribute_evidence":
        return await store.contribute_evidence(
            str(args.get("case_id") or ""),
            args.get("evidence") or {},
            contributor=str(args.get("contributor") or "remote"),
        )
    raise KeyError(skill)


async def _route_investigate(args: dict[str, Any]) -> Any:
    """Route investigate_dispute to the agent that actually RUNS cases.

    With the NOTIFY worker gone, the generic dispatcher's
    create_investigation would insert a case nobody investigates. Instead:
    INVESTIGATOR_A2A_URL set -> real A2A hop; unset -> the investigator's
    in-process handler (local dev mode, same as triage's fallback)."""
    url = os.environ.get("INVESTIGATOR_A2A_URL")
    if url:
        from manthan_agent.a2a.client import call_skill

        result = call_skill(
            url, "investigate_dispute", args,
            api_key=os.environ.get("INVESTIGATOR_A2A_API_KEY"),
        )
        if inspect.isawaitable(result):
            result = await result
        return result
    from manthan_api.agents import investigator

    return await investigator.handle_investigate(args, actor="a2a:remote")


async def dispatch_advisor(payload: dict[str, Any], store: Any) -> dict[str, Any]:
    """JSON-RPC dispatch: advisor skills here, investigate_dispute routed to
    the investigator, everything else (the 6 read skills, tasks/get,
    protocol errors) via the shared agent-package dispatcher over the same
    store."""
    if isinstance(payload, dict) and payload.get("method") == "message/send":
        skill, args = extract_skill(payload.get("params", {}) or {})
        req_id = payload.get("id")
        if skill in ADVISOR_SKILL_IDS:
            try:
                result = await _run_advisor_skill(skill, args, store)
            except Exception as exc:  # noqa: BLE001
                return rpc_err(
                    req_id, JSONRPC_INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"
                )
            return rpc_ok(req_id, data_message(result))
        if skill == "investigate_dispute":
            try:
                result = await _route_investigate(args)
            except Exception as exc:  # noqa: BLE001
                return rpc_err(
                    req_id, JSONRPC_INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"
                )
            return rpc_ok(req_id, result)
    return await dispatch(payload, store)


# ──────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────

# Lazily constructed so importing the app never touches the DB. Tests swap
# this for an in-memory store via monkeypatch.
_store: Any = None


def get_store() -> Any:
    global _store
    if _store is None:
        _store = PgCaseStore()
    return _store


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
        await close_pool()


app = FastAPI(title="Manthan Advisor (A2A)", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "agent": AGENT_ID}


@app.get("/.well-known/agent-card.json")
async def agent_card(request: Request) -> dict[str, Any]:
    identity = agent_identity(
        AGENT_ID, os.environ.get("MANTHAN_MODEL_SUBAGENT") or "gemini-3.5-flash"
    )
    card = build_agent_card(public_base_url(request), identity=identity)
    card["name"] = "Manthan Advisor"
    card["description"] = (
        "Conversational face of Manthan for other agents: ask cited questions "
        "about cases, pre-check refunds before granting them, pull customer "
        "history and exposure aggregates, and contribute evidence into open "
        "investigations."
    )
    # Advisor skills + the 6 read skills (no investigate_dispute - that's
    # the investigator's job; the card keeps each agent's mandate narrow).
    # The shared query list also carries ask/precheck/history/exposure -
    # ADVISOR_SKILLS owns those entries, so drop them from the merge.
    advisor_ids = {s["id"] for s in ADVISOR_SKILLS}
    card["skills"] = ADVISOR_SKILLS + [
        s
        for s in card["skills"]
        if s["id"] in QUERY_SKILL_IDS and s["id"] not in advisor_ids
    ]
    return card


@app.post("/a2a")
async def a2a_rpc(request: Request) -> dict[str, Any]:
    payload = await parse_jsonrpc(request)
    if payload is None:
        return rpc_err(None, JSONRPC_PARSE_ERROR, "invalid JSON")
    return await dispatch_advisor(payload, get_store())
