"""Postgres-backed CaseStore for the A2A surface.

`PgCaseStore` duck-types `manthan_agent.a2a.store.CaseStore` and extends it
with the advisor skill surface: every A2A query skill (get_case, list_cases,
get_brief, get_findings, get_actions, get_audit_trail) reads straight from the
cases/events/findings/actions projections; the advisor skills (ask,
precheck_refund, get_customer_history, dispute_exposure, contribute_evidence)
layer Q&A + collaboration on the same tables; and the investigate_dispute
action funnels into the exact same case-creation flow the web "+ New" button
uses (cases insert + `case_opened` event). The retired investigate worker's
NOTIFY hop is gone - the investigator agent service runs the investigation
in-process (manthan_api.agents.investigator) and writes its own events.

Org scoping: A2A traffic has no Clerk member context, so the store binds to a
single org resolved once from the MANTHAN_A2A_ORG slug (default: the `acme`
dev org seeded by scripts/bootstrap_dev_org.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from typing import Any
from uuid import UUID

from manthan_api.db import get_conn

# Matches scripts/bootstrap_dev_org.py DEV_ORG_SLUG.
DEFAULT_A2A_ORG_SLUG = "acme"

# Case statuses that count as "open" for advisor aggregates/prechecks.
OPEN_STATUSES = ("investigating", "awaiting_approval", "acting", "escalated")


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
# advisor helpers - pure rules + the one-shot grounded LLM call
#
# These are module-level (not methods) so the advisor's unit tests and the
# in-memory test store exercise the exact same logic without Postgres.
# ──────────────────────────────────────────────────────────────────────


def precheck_recommendation(
    prior_disputes: int,
    open_cases: int,
    outcomes: dict[str, int],
    *,
    amount_minor: int | None = None,
) -> str:
    """Deterministic refund-precheck rules - no LLM, no I/O.

      1. any OPEN case for this customer            -> investigate_first
      2. >=3 prior disputes OR >=2 'fight' verdicts -> high_risk
      3. zero prior disputes AND amount < $500
         (or amount unknown)                        -> low_risk
      4. everything else                            -> investigate_first
    """
    if open_cases > 0:
        return "investigate_first"
    if prior_disputes >= 3 or outcomes.get("fight", 0) >= 2:
        return "high_risk"
    if prior_disputes == 0 and (amount_minor is None or amount_minor < 50000):
        return "low_risk"
    return "investigate_first"


ASK_SYSTEM = """\
You are Manthan's advisor, answering another agent's (or operator's) question \
about a billing case. The CONTEXT block carries the case's recorded findings \
(numbered) and the drafted brief. Answer ONLY from that context.

Rules:
- Cite finding indices inline like [1] or [2][4] for every claim you make.
- If the context cannot answer the question, say so plainly - never invent data.
- 2-5 sentences, plain English, no engineering jargon.
"""

LLM_UNAVAILABLE_ANSWER = (
    "LLM unavailable - GOOGLE_API_KEY is not configured on this service, so I "
    "can't synthesize an answer. The raw case record is still readable via the "
    "get_findings / get_brief / get_audit_trail skills."
)


def build_ask_grounding(
    findings: list[dict[str, Any]],
    brief: dict[str, Any] | None,
    *,
    case_label: str = "",
) -> str:
    """Render findings + brief into the CONTEXT block ask() grounds on."""
    lines: list[str] = []
    if case_label:
        lines.append(f"CASE: {case_label}")
    lines.append("FINDINGS:")
    if findings:
        for i, f in enumerate(findings, start=1):
            if not isinstance(f, dict):
                continue
            seq = f.get("seq", i)
            conf = f.get("confidence")
            conf_str = f" (conf {conf:.2f})" if isinstance(conf, (int, float)) else ""
            lines.append(f"[{seq}]{conf_str} {f.get('text', '')}")
    else:
        lines.append("(no findings recorded)")
    if isinstance(brief, dict):
        if brief.get("tldr"):
            lines.append(f"BRIEF TL;DR: {str(brief['tldr'])[:800]}")
        decision = brief.get("decision")
        if isinstance(decision, dict):
            lines.append(
                "DECISION: "
                f"{decision.get('action')} "
                f"amount_minor={decision.get('amount_minor')} "
                f"confidence={decision.get('confidence')}"
            )
    return "\n".join(lines)


async def grounded_answer(
    question: str,
    *,
    context_block: str,
    system: str = ASK_SYSTEM,
    model: str | None = None,
) -> tuple[str, bool]:
    """ONE Gemini call grounded on the supplied context.

    Returns (answer, grounded). When GOOGLE_API_KEY is missing (or the call
    fails) the answer degrades to an explicit "LLM unavailable" message with
    grounded=False instead of raising - the A2A surface must stay up even
    on an un-keyed deployment.
    """
    # Lazy import so importing this module never requires google-genai at
    # collection time; module-attribute access keeps monkeypatching of
    # manthan_agent.llm.generate_text effective in tests.
    from manthan_agent import config as agent_config
    from manthan_agent import llm as agent_llm

    cfg = agent_config.load()
    user = f"CONTEXT:\n{context_block}\n\nQUESTION: {question}"
    try:
        text = await asyncio.to_thread(
            agent_llm.generate_text,
            cfg,
            user=user,
            system=system,
            model=model or cfg.model_triage,
            temperature=0.2,
            max_output_tokens=512,
        )
        return (text or "(no reply)"), True
    except agent_llm.LLMNotConfigured:
        return LLM_UNAVAILABLE_ANSWER, False
    except Exception as exc:  # noqa: BLE001 - keep the A2A surface alive
        return f"LLM unavailable ({type(exc).__name__}: {exc}).", False


async def run_ask(
    question: str,
    *,
    findings: list[dict[str, Any]],
    brief: dict[str, Any] | None,
    case_label: str = "",
) -> dict[str, Any]:
    """The shared ask() implementation: ground on findings+brief, one call."""
    grounding = build_ask_grounding(findings, brief, case_label=case_label)
    answer, grounded = await grounded_answer(question, context_block=grounding)
    return {
        "question": question,
        "answer": answer,
        "grounded": grounded,
        "findings_used": len(findings),
        "case": case_label or None,
    }


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

    # ---- advisor skills (conversational / collaborate surface) ----

    async def ask(
        self, question: str, case_id: str | None = None
    ) -> dict[str, Any]:
        """ONE Gemini call grounded on the case's findings + brief from PG.

        v1 is case-scoped and Coral-free: the grounding is whatever the
        investigator already recorded. Citations come back as finding
        indices ([1], [3]...) that map to get_findings seq numbers.
        """
        if not case_id:
            return {
                "question": question,
                "answer": (
                    "Provide a case_id - v1 ask() answers per-case questions "
                    "grounded on that case's recorded findings and brief."
                ),
                "grounded": False,
                "findings_used": 0,
                "case": None,
            }
        case = await self.get_case(case_id)
        if case is None:
            return {
                "question": question,
                "answer": f"Unknown case '{case_id}'.",
                "grounded": False,
                "findings_used": 0,
                "case": None,
            }
        findings = await self.get_findings(case_id)
        brief = await self.get_brief(case_id)
        return await run_ask(
            question,
            findings=findings,
            brief=brief,
            case_label=case.get("short_id") or case_id,
        )

    async def precheck_refund(
        self, customer_ref: str, amount_minor: int | None = None
    ) -> dict[str, Any]:
        """Pre-refund risk check for an external CS/refund-desk agent.

        Pure SQL over this org's cases for the customer_ref; the
        recommendation is derived by precheck_recommendation (deterministic
        rules, no LLM)."""
        if not customer_ref:
            return {"error": "customer_ref is required"}
        org_id = await self._org()
        async with get_conn() as conn:
            rows = await conn.fetch(
                """
                SELECT status, decision_action
                FROM cases
                WHERE org_id = $1 AND customer_ref = $2
                ORDER BY created_at DESC
                LIMIT 200
                """,
                org_id,
                customer_ref,
            )
        prior_disputes = len(rows)
        open_cases = sum(1 for r in rows if r["status"] in OPEN_STATUSES)
        outcomes: dict[str, int] = {}
        for r in rows:
            if r["decision_action"]:
                outcomes[r["decision_action"]] = outcomes.get(r["decision_action"], 0) + 1
        return {
            "customer_ref": customer_ref,
            "amount_minor": amount_minor,
            "prior_disputes": prior_disputes,
            "open_cases": open_cases,
            "outcomes": outcomes,
            "recommendation": precheck_recommendation(
                prior_disputes, open_cases, outcomes, amount_minor=amount_minor
            ),
        }

    async def get_customer_history(
        self, customer_ref: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Episodic memory by customer_ref: this customer's cases + decisions."""
        if not customer_ref:
            return []
        org_id = await self._org()
        limit = max(1, min(int(limit or 20), 100))
        async with get_conn() as conn:
            rows = await conn.fetch(
                """
                SELECT id, short_id, status, case_type, customer_ref, amount_minor,
                       decision_action, decision_amount_minor, decision_confidence,
                       created_at
                FROM cases
                WHERE org_id = $1 AND customer_ref = $2
                ORDER BY created_at DESC
                LIMIT $3
                """,
                org_id,
                customer_ref,
                limit,
            )
        out: list[dict[str, Any]] = []
        for r in rows:
            proj = _case_projection(r)
            proj["case_type"] = r["case_type"]
            proj["amount_minor"] = r["amount_minor"]
            proj["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
            out.append(proj)
        return out

    async def dispute_exposure(self) -> dict[str, Any]:
        """Aggregate exposure for CFO-style agents: open case count + the
        sum of decision_amount_minor grouped by status."""
        org_id = await self._org()
        async with get_conn() as conn:
            rows = await conn.fetch(
                """
                SELECT status,
                       COUNT(*)                              AS cases,
                       COALESCE(SUM(decision_amount_minor), 0) AS decision_amount_minor
                FROM cases
                WHERE org_id = $1
                GROUP BY status
                """,
                org_id,
            )
        by_status = {
            r["status"]: {
                "cases": int(r["cases"]),
                "decision_amount_minor": int(r["decision_amount_minor"]),
            }
            for r in rows
        }
        open_cases = sum(v["cases"] for s, v in by_status.items() if s in OPEN_STATUSES)
        open_amount = sum(
            v["decision_amount_minor"] for s, v in by_status.items() if s in OPEN_STATUSES
        )
        return {
            "open_cases": open_cases,
            "open_decision_amount_minor": open_amount,
            "by_status": by_status,
        }

    async def contribute_evidence(
        self,
        case_id: str,
        evidence: dict[str, Any],
        contributor: str = "remote",
    ) -> dict[str, Any]:
        """External agent pushes evidence onto an open case's thread.

        Appends an `evidence_contributed` event with actor 'a2a:<contributor>'
        so provenance is visible in the audit trail; the investigator (or an
        operator) picks it up from there."""
        cid = _to_uuid(case_id)
        if cid is None:
            return {"error": f"invalid case_id '{case_id}'"}
        org_id = await self._org()
        async with get_conn() as conn:
            thread_id = await self._thread_id(conn, org_id, cid)
        if thread_id is None:
            return {"error": f"unknown case '{case_id}'"}
        data = evidence if isinstance(evidence, dict) else {"text": str(evidence)}
        actor = f"a2a:{contributor or 'remote'}"
        from manthan_api.services.case_store import append_event as _append_event

        await _append_event(org_id, thread_id, "evidence_contributed", actor, data)
        return {
            "contributed": True,
            "case_id": case_id,
            "type": "evidence_contributed",
            "actor": actor,
        }
