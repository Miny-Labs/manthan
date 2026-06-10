"""Postgres-backed CaseStore for the A2A surface.

`PgCaseStore` duck-types `manthan_agent.a2a.store.CaseStore`: every A2A query
skill (get_case, list_cases, get_brief, get_findings, get_actions,
get_audit_trail) reads straight from the cases/events/findings/actions
projections, and the investigate_dispute action funnels into the exact same
case-creation flow the web "+ New" button uses (cases insert + `case_opened`
event; the `events_notify` trigger fires PG NOTIFY 'manthan_event' so the
investigate worker picks the case up).

Org scoping: A2A traffic has no Clerk member context, so the store binds to a
single org resolved once from the MANTHAN_A2A_ORG slug (default: the `acme`
dev org seeded by scripts/bootstrap_dev_org.py).
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from typing import Any
from uuid import UUID

from manthan_api.db import get_conn

# Matches scripts/bootstrap_dev_org.py DEV_ORG_SLUG.
DEFAULT_A2A_ORG_SLUG = "acme"


def _default_org_slug() -> str:
    return os.environ.get("MANTHAN_A2A_ORG") or DEFAULT_A2A_ORG_SLUG


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────


def _to_uuid(case_id: Any) -> UUID | None:
    """Parse a case id, returning None (not raising) for junk input."""
    try:
        return UUID(str(case_id))
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> float | None:
    """NUMERIC columns come back as Decimal - make them JSON-friendly."""
    return float(value) if value is not None else None


def _jsonish(value: Any) -> Any:
    """JSONB columns normally decode to dict/list via the pool codec, but
    tolerate raw-string rows (e.g. legacy double-encoded payloads)."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _case_projection(row: Any) -> dict[str, Any]:
    # decision must be None (not a dict of Nones) until the agent actually
    # decides — A2A's tasks/get derives state "completed" from its truthiness.
    decision = None
    if row["decision_action"] is not None:
        decision = {
            "action": row["decision_action"],
            "amount_minor": row["decision_amount_minor"],
            "confidence": _num(row["decision_confidence"]),
        }
    return {
        "case_id": str(row["id"]),
        "short_id": row["short_id"],
        "status": row["status"],
        "customer_ref": row["customer_ref"],
        "decision": decision,
    }


def next_short_id() -> str:
    """Human-friendly display id, same convention as api/cases.py
    (`CASE-` + random 4 digits)."""
    return f"CASE-{secrets.randbelow(9000) + 1000}"


def _amount_from_structured(structured: dict[str, Any]) -> tuple[int | None, str | None]:
    """Best-effort (amount_minor, currency) off a trigger's structured dict.

    The triage contract nests the raw Stripe object under structured["object"]
    (aliased to "event_object" for the worker) — check those levels too, or
    webhook-opened cases lose amount_minor and the policy engine's amount
    thresholds (auto / one-click / two-person) silently never match.
    """
    candidates: list[Any] = [
        structured,
        structured.get("object"),
        structured.get("event_object"),
        structured.get("charge_details"),
    ]
    for d in candidates:
        if not isinstance(d, dict):
            continue
        for key in ("amount_minor", "amount", "amount_due"):
            v = d.get(key)
            if isinstance(v, int):
                cur = d.get("currency")
                return v, cur.lower() if isinstance(cur, str) else None
    return None, None


# ──────────────────────────────────────────────────────────────────────
# shared case-creation flow
# ──────────────────────────────────────────────────────────────────────


async def insert_case_from_trigger(
    org_id: UUID,
    trigger: dict[str, Any],
    *,
    actor: str = "a2a:remote",
    short_id: str | None = None,
    amount_minor: int | None = None,
    currency: str | None = None,
    extra_event_data: dict[str, Any] | None = None,
) -> tuple[UUID, str]:
    """Open a case from a trigger dict - the same two inserts POST /api/cases
    performs (cases row + seq-1 `case_opened` event). The events insert fires
    the `events_notify` trigger, so the NOTIFY payload shape is identical.

    Trigger keys (cross-agent contract): text, case_type, customer_ref,
    structured, source_surface.
    """
    trigger_text = trigger.get("text") or ""
    case_type = trigger.get("case_type")
    customer_ref = trigger.get("customer_ref")
    structured = trigger.get("structured") or {}
    surface = trigger.get("source_surface") or "api"

    if amount_minor is None:
        amount_minor, structured_currency = _amount_from_structured(structured)
        currency = currency or structured_currency
    currency = currency or "usd"

    thread_id = uuid.uuid4()
    sid = short_id or next_short_id()

    async with get_conn() as conn:
        async with conn.transaction():
            case_row = await conn.fetchrow(
                """
                INSERT INTO cases (
                    org_id, thread_id, short_id, status, trigger_surface,
                    trigger_payload, case_type, customer_ref, amount_minor, currency
                )
                VALUES ($1, $2, $3, 'investigating', $4, $5, $6, $7, $8, $9)
                RETURNING id
                """,
                org_id,
                thread_id,
                sid,
                surface,
                # asyncpg JSONB codec serializes dicts - don't json.dumps.
                structured,
                case_type,
                customer_ref,
                amount_minor,
                currency,
            )
            case_id = case_row["id"]

            # case_opened - the investigate worker reacts via LISTEN/NOTIFY.
            data: dict[str, Any] = {
                "case_id": str(case_id),
                "short_id": sid,
                "trigger_surface": surface,
                "trigger_text": trigger_text,
                "case_type": case_type,
                "customer_ref": customer_ref,
                "amount_minor": amount_minor,
            }
            if extra_event_data:
                data.update(extra_event_data)
            await conn.execute(
                """
                INSERT INTO events (org_id, thread_id, seq, type, actor, data)
                VALUES ($1, $2, 1, 'case_opened', $3, $4)
                """,
                org_id,
                thread_id,
                actor,
                data,
            )

    return case_id, sid


# ──────────────────────────────────────────────────────────────────────
# the store
# ──────────────────────────────────────────────────────────────────────


class PgCaseStore:
    """Postgres implementation of the A2A CaseStore protocol (duck-typed -
    see manthan_agent.a2a.store.CaseStore for the contract)."""

    def __init__(self, org_slug: str | None = None) -> None:
        self._org_slug = org_slug or _default_org_slug()
        self._org_id: UUID | None = None

    async def _org(self) -> UUID:
        """Resolve (once) the org this A2A surface serves."""
        if self._org_id is None:
            async with get_conn() as conn:
                row = await conn.fetchrow(
                    "SELECT id FROM orgs WHERE slug = $1", self._org_slug
                )
            if row is None:
                raise RuntimeError(
                    f"A2A org not found: {self._org_slug!r} - set MANTHAN_A2A_ORG "
                    "or run scripts/bootstrap_dev_org.py"
                )
            self._org_id = row["id"]
        return self._org_id

    async def _thread_id(self, conn: Any, org_id: UUID, case_uuid: UUID) -> UUID | None:
        return await conn.fetchval(
            "SELECT thread_id FROM cases WHERE org_id = $1 AND id = $2",
            org_id,
            case_uuid,
        )

    # ---- writes ----

    async def create_investigation(self, trigger: dict[str, Any]) -> str:
        org_id = await self._org()
        case_id, _short_id = await insert_case_from_trigger(org_id, trigger)
        return str(case_id)

    # ---- reads ----

    async def get_case(self, case_id: str) -> dict[str, Any] | None:
        cid = _to_uuid(case_id)
        if cid is None:
            return None
        org_id = await self._org()
        async with get_conn() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, short_id, status, customer_ref,
                       decision_action, decision_amount_minor, decision_confidence
                FROM cases
                WHERE org_id = $1 AND id = $2
                """,
                org_id,
                cid,
            )
        return _case_projection(row) if row else None

    async def list_cases(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        org_id = await self._org()
        limit = max(1, min(int(limit or 50), 200))
        where = ["org_id = $1"]
        params: list[Any] = [org_id]
        if status:
            where.append(f"status = ${len(params) + 1}")
            params.append(status)
        async with get_conn() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, short_id, status, customer_ref,
                       decision_action, decision_amount_minor, decision_confidence
                FROM cases
                WHERE {' AND '.join(where)}
                ORDER BY created_at DESC
                LIMIT ${len(params) + 1}
                """,
                *params,
                limit,
            )
        return [_case_projection(r) for r in rows]

    async def get_brief(self, case_id: str) -> dict[str, Any] | None:
        cid = _to_uuid(case_id)
        if cid is None:
            return None
        org_id = await self._org()
        async with get_conn() as conn:
            thread_id = await self._thread_id(conn, org_id, cid)
            if thread_id is None:
                return None
            row = await conn.fetchrow(
                """
                SELECT data FROM events
                WHERE org_id = $1 AND thread_id = $2 AND type = 'brief_drafted'
                ORDER BY seq DESC
                LIMIT 1
                """,
                org_id,
                thread_id,
            )
        if row is None:
            return None
        data = _jsonish(row["data"])
        return data if isinstance(data, dict) else {"brief": data}

    async def get_findings(self, case_id: str) -> list[dict[str, Any]]:
        cid = _to_uuid(case_id)
        if cid is None:
            return []
        org_id = await self._org()
        async with get_conn() as conn:
            rows = await conn.fetch(
                """
                SELECT seq, text, confidence, citations
                FROM findings
                WHERE org_id = $1 AND case_id = $2
                ORDER BY seq ASC
                """,
                org_id,
                cid,
            )
        return [
            {
                "seq": r["seq"],
                "text": r["text"],
                "confidence": _num(r["confidence"]),
                "citations": _jsonish(r["citations"]) or [],
            }
            for r in rows
        ]

    async def get_actions(self, case_id: str) -> list[dict[str, Any]]:
        cid = _to_uuid(case_id)
        if cid is None:
            return []
        org_id = await self._org()
        async with get_conn() as conn:
            rows = await conn.fetch(
                """
                SELECT seq, type, payload, status
                FROM actions
                WHERE org_id = $1 AND case_id = $2
                ORDER BY seq ASC
                """,
                org_id,
                cid,
            )
        return [
            {
                "seq": r["seq"],
                "kind": r["type"],
                "payload": _jsonish(r["payload"]),
                "status": r["status"],
            }
            for r in rows
        ]

    async def get_audit_trail(self, case_id: str) -> list[dict[str, Any]]:
        cid = _to_uuid(case_id)
        if cid is None:
            return []
        org_id = await self._org()
        async with get_conn() as conn:
            thread_id = await self._thread_id(conn, org_id, cid)
            if thread_id is None:
                return []
            rows = await conn.fetch(
                """
                SELECT seq, type, actor, data, created_at
                FROM events
                WHERE org_id = $1 AND thread_id = $2
                ORDER BY seq ASC
                """,
                org_id,
                thread_id,
            )
        return [
            {
                "seq": r["seq"],
                "type": r["type"],
                "actor": r["actor"],
                "data": _jsonish(r["data"]),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
