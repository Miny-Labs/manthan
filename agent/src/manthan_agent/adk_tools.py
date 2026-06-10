"""ADK tool factory for the Investigator agent.

The custom loop's `tools.py` defined Pydantic-schema'd tools dispatched by a
hand-rolled executor. Under ADK the agent calls plain Python functions and
ADK builds the Gemini function declarations from their signatures + Google-
style docstrings. This module returns those functions as closures bound to a
per-run `RunState` and the live Coral MCP session.

What's preserved verbatim from the old design:
  * the six tools (coral_sql / coral_list_catalog / coral_describe_table /
    record_finding / ask_human / conclude)
  * Evidence accumulation with full provenance (source, table, record_id)
  * integer citation indices into the Evidence list — the contract the brief's
    citation chips ("click → source row") depend on
  * the defensive `conclude` parsing that clamps the decision and drops
    malformed drafted actions instead of crashing the brief

Why closures (not module globals / contextvars): each run_case() builds its
own tool set bound to that run's Coral session + Evidence list, so concurrent
cases never share state and ADK's task scheduling can't lose a contextvar.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from google.adk.tools import ToolContext

from .types import Brief, Decision, DraftedAction, Evidence, Finding

# Schemas Coral exposes per source — used to tag which brand pill a query's
# Evidence chip should show. Mirrors the set in the old tools.py.
_KNOWN_SOURCES: set[str] = {
    "stripe", "chargebee", "razorpay", "hubspot", "salesforce", "intercom",
    "zendesk", "slack", "notion", "confluence", "gmail", "postmark", "resend",
    "mailchimp", "loops", "twilio", "clerk", "cal", "mixpanel", "posthog",
    "sentry", "datadog", "grafana", "pagerduty", "statusgator", "launchdarkly",
    "github", "linear", "gitlab", "google_drive", "k8s",
}

import re

_SOURCE_REF_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\s*\.\s*[a-z_][a-z0-9_]*")

_VALID_ACTIONS = ("fight", "refund", "accept", "escalate")
_VALID_KINDS = (
    "stripe_refund", "stripe_dispute_response", "customer_email",
    "hubspot_note", "slack_brief",
)


def _sources_in_query(q: str) -> list[str]:
    return sorted({
        m.group(1)
        for m in _SOURCE_REF_RE.finditer(q.lower())
        if m.group(1) in _KNOWN_SOURCES
    })


def _mcp_text(call_result: Any) -> str:
    """Pull the first text content block from an MCP call result."""
    if not getattr(call_result, "content", None):
        return ""
    first = call_result.content[0]
    return getattr(first, "text", str(first))


# ──────────────────────────────────────────────────────────────────────
# Per-run state — replaces the old ToolExecutor instance + loop locals.
# ──────────────────────────────────────────────────────────────────────


@dataclass
class RunState:
    """Mutable state for a single investigation, shared by its tool closures.

    `evidence` and `findings` accumulate as the agent works; `brief` and
    `terminal` are populated when the agent calls conclude / ask_human.
    The run_case driver reads these to emit the Event stream + final Brief.
    """

    evidence: list[Evidence] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    brief: Brief | None = None
    terminal: dict[str, Any] | None = None  # {"kind": "conclude"|"ask_human", ...}
    # Tool-call telemetry the run_case translator + pacer read.
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # Pacer nudges / halts to surface as agent_thought events.
    pacer_log: list[dict[str, Any]] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────
# Tool factory
# ──────────────────────────────────────────────────────────────────────


def build_tools(coral_session: Any, catalog_tool: str, state: RunState) -> list[Any]:
    """Return the six investigator tools as closures bound to this run.

    `coral_session` is a live MCP ClientSession (already initialised).
    `catalog_tool` is "list_catalog" (Coral 0.4.x) or "list_tables" (0.3.x).
    `state` accumulates Evidence / Findings / the final Brief.
    """

    async def coral_sql(query: str) -> dict:
        """Execute read-only SQL against Coral's unified SQL plane.

        Coral exposes every connected source (stripe, intercom, notion,
        datadog, slack, posthog, sentry, pagerduty, salesforce, ...) as
        Postgres-style schemas. Prefer ONE within-source JOIN over many
        single-table queries. Returns rows plus an evidence_indices list you
        cite from record_finding.

        Args:
            query: the SQL statement to run (e.g. "SELECT d.id, d.amount FROM
                stripe.disputes d WHERE d.id = 'du_...'").
        """
        sources_used = _sources_in_query(query)
        call_result = await coral_session.call_tool("sql", arguments={"sql": query})

        if getattr(call_result, "isError", False):
            err_text = _mcp_text(call_result) or "Coral returned isError=True"
            ev = Evidence(
                source="coral_error",
                table=",".join(sources_used) or "(unknown)",
                record_id=f"err_{len(state.evidence):02d}",
                fields={"sql_error": err_text, "query": query},
                query=query,
                retrieved_at=datetime.utcnow(),
            )
            idx = len(state.evidence)
            state.evidence.append(ev)
            return {
                "status": "error",
                "error": err_text,
                "hint": "SQL error from Coral. Check table/column names with "
                        "coral_describe_table, or try a different shape.",
                "evidence_indices": [idx],
            }

        raw = _mcp_text(call_result)
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = {"rows": [], "_raw": raw}

        rows: list[dict[str, Any]] = []
        if isinstance(parsed, list):
            rows = [r for r in parsed if isinstance(r, dict)]
        elif isinstance(parsed, dict):
            candidate = parsed.get("rows") or parsed.get("result") or parsed.get("items")
            if isinstance(candidate, list):
                rows = [r for r in candidate if isinstance(r, dict)]
        cols = list(rows[0].keys()) if rows else []

        is_join = len(sources_used) > 1
        source_label = "coral_join" if is_join else (sources_used[0] if sources_used else "coral")
        table_label = "+".join(sources_used) if is_join else (sources_used[0] if sources_used else "(none)")

        ev = Evidence(
            source=source_label,
            table=table_label,
            record_id=f"q_{len(state.evidence):02d}",
            fields={"columns": cols, "rows": rows, "row_count": len(rows)},
            query=query,
            retrieved_at=datetime.utcnow(),
        )
        idx = len(state.evidence)
        state.evidence.append(ev)

        return {
            "status": "ok",
            "columns": cols,
            "rows": rows,
            "row_count": len(rows),
            "evidence_indices": [idx],
            "sources_joined": sources_used,
        }

    async def coral_list_catalog() -> dict:
        """List the schemas + tables Coral can see for THIS case.

        Call this once at the start to learn which sources are connected —
        availability varies per case. Never assume a source exists.
        """
        args = {"limit": 200} if catalog_tool == "list_catalog" else {}
        call_result = await coral_session.call_tool(catalog_tool, arguments=args)
        raw = _mcp_text(call_result)
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = {}

        items = []
        if isinstance(parsed, dict):
            items = parsed.get("items") or parsed.get("tables") or parsed.get("rows") or []
        elif isinstance(parsed, list):
            items = parsed

        schemas: dict[str, list[str]] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            name = it.get("name") or it.get("table") or it.get("sql_reference") or ""
            schema = (
                it.get("schema_name") or it.get("schema") or it.get("source")
                or (name.split(".", 1)[0] if "." in name else "")
            )
            table_info = it.get("table") if isinstance(it.get("table"), dict) else {}
            bare = (
                (table_info.get("table_name") if table_info else None)
                or (name.split(".", 1)[-1] if "." in name else name)
            )
            if schema and bare:
                schemas.setdefault(schema, []).append(bare)

        return {
            "status": "ok",
            "schemas": [
                {"name": s, "tables": sorted(set(ts))} for s, ts in sorted(schemas.items())
            ],
            "note": "Live catalog from Coral.",
        }

    async def coral_describe_table(qualified_name: str) -> dict:
        """Return the columns + types for one Coral table.

        Use before composing SQL against a table you haven't queried.

        Args:
            qualified_name: the schema-qualified table, e.g. "stripe.disputes".
        """
        if "." not in qualified_name:
            return {
                "status": "error",
                "error": f"qualified_name must be 'schema.table', got '{qualified_name}'",
                "hint": "Pass e.g. 'stripe.disputes', not just 'disputes'.",
            }
        schema_name, table_name = qualified_name.split(".", 1)
        # Coral 0.4.x: list_columns; 0.3.x has neither — fall back to a 1-row
        # SELECT and read the keys.
        try:
            call_result = await coral_session.call_tool(
                "list_columns", arguments={"schema": schema_name, "table": table_name}
            )
            raw = _mcp_text(call_result)
            parsed = json.loads(raw)
            cols_raw = parsed.get("items") or parsed.get("columns") or []
            columns = [
                {"name": c.get("name") or c.get("column_name"),
                 "type": c.get("type") or c.get("data_type")}
                for c in cols_raw if isinstance(c, dict)
            ]
            if columns:
                return {"status": "ok", "table": qualified_name, "columns": columns}
        except Exception:  # noqa: BLE001 — fall through to the SELECT probe
            pass

        probe = await coral_session.call_tool(
            "sql", arguments={"sql": f"SELECT * FROM {qualified_name} LIMIT 1"}
        )
        raw = _mcp_text(probe)
        try:
            parsed = json.loads(raw)
            rows = parsed.get("rows") if isinstance(parsed, dict) else parsed
            cols = list(rows[0].keys()) if rows else []
        except Exception:  # noqa: BLE001
            cols = []
        return {
            "status": "ok",
            "table": qualified_name,
            "columns": [{"name": c, "type": "?"} for c in cols],
            "note": "Columns inferred from a 1-row probe.",
        }

    async def record_finding(text: str, citations: list[int], confidence: float) -> dict:
        """Assert ONE cited, plain-English factual claim for the brief.

        Lead with a finance-team sentence, then the precise evidence in
        parentheses. e.g. "The disputed charge is $8,400 for the customer's
        April cycle. (Stripe charge ch_..., disputed via du_...)." Every
        finding needs >=1 citation — an integer index from a prior
        coral_sql result's evidence_indices.

        Args:
            text: one plain-English claim + parenthetical evidence.
            citations: evidence indices this claim rests on (>=1).
            confidence: 0..1 — 0.95+ direct read-out, 0.7-0.9 inference.
        """
        citations = [c for c in (citations or []) if isinstance(c, int)]
        try:
            conf = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            conf = 0.5
        if not citations:
            return {
                "status": "error",
                "error": "record_finding needs >=1 integer citation index.",
                "hint": "Cite an index from a coral_sql result's evidence_indices.",
            }

        state.findings.append(Finding(text=text, citations=citations, confidence=conf))
        resolved: list[dict[str, Any]] = []
        for idx in citations:
            if 0 <= idx < len(state.evidence):
                ev = state.evidence[idx]
                resolved.append({
                    "idx": idx, "source": ev.source, "table": ev.table,
                    "ref": ev.record_id, "field": None,
                })
        return {
            "status": "ok",
            "finding_index": len(state.findings) - 1,
            "citations_resolved": resolved,
            "total_findings": len(state.findings),
        }

    def ask_human(question: str, recommendation: str, confidence: float,
                  options: list[str] | None = None,
                  tool_context: ToolContext = None) -> dict:
        """Pause and escalate to a human with a decision-quality question.

        Use when evidence genuinely contradicts, confidence is below ~0.7 on
        a high-stakes call, or the case is novel. Not for "approve?".

        Args:
            question: the decision-quality question (names the tradeoff).
            recommendation: what you think the right action is.
            confidence: 0..1.
            options: named alternatives the human can pick.
        """
        try:
            conf = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            conf = 0.5
        state.terminal = {
            "kind": "ask_human",
            "question": question,
            "recommendation": recommendation,
            "confidence": conf,
            "options": options or [],
        }
        if tool_context is not None:
            tool_context.actions.escalate = True  # end the run cleanly
        return {"status": "ok", "acknowledged": "escalated_to_human"}

    async def conclude(
        tldr: str,
        decision_action: str,
        decision_rationale: str,
        decision_confidence: float,
        hitl_question: str,
        decision_amount_minor: int = 0,
        decision_currency: str = "usd",
        drafted_actions_json: str = "[]",
        tool_context: ToolContext = None,
    ) -> dict:
        """Finalise the investigation: emit the Brief and end.

        Call when confidence is high enough to recommend OR evidence is
        saturated. Draft the COMPLETE set of actions for the decision.

        Args:
            tldr: 3-5 sentence CFO-facing summary (plain English, money with
                commas, math in words, no raw IDs/enums in the lede).
            decision_action: one of fight | refund | accept | escalate.
            decision_rationale: 2-4 sentences citing finding numbers [1][2].
            decision_confidence: 0..1.
            hitl_question: decision-quality question naming the tradeoff.
            decision_amount_minor: amount in MINOR units (cents). $4,200 ->
                420000. Leave 0 for fight/escalate.
            decision_currency: ISO 4217, e.g. "usd".
            drafted_actions_json: JSON array string of DraftedAction objects,
                each {"kind","payload","description","reversibility"}. kind in
                stripe_refund|stripe_dispute_response|customer_email|
                hubspot_note|slack_brief. No empty/TODO payloads.
        """
        action = decision_action if decision_action in _VALID_ACTIONS else "escalate"
        try:
            conf = max(0.0, min(1.0, float(decision_confidence)))
        except (TypeError, ValueError):
            conf = 0.5
        amount = decision_amount_minor if isinstance(decision_amount_minor, int) and decision_amount_minor > 0 else None

        # Defensive parse of drafted actions: skip malformed entries.
        try:
            raw_actions = json.loads(drafted_actions_json or "[]")
        except (json.JSONDecodeError, TypeError):
            raw_actions = []
        if not isinstance(raw_actions, list):
            raw_actions = []
        safe_actions: list[DraftedAction] = []
        dropped = 0
        for raw in raw_actions:
            if not isinstance(raw, dict) or raw.get("kind") not in _VALID_KINDS:
                dropped += 1
                continue
            try:
                safe_actions.append(DraftedAction(
                    kind=raw["kind"],
                    payload=raw.get("payload") or {},
                    description=raw.get("description", ""),
                    reversibility=raw.get("reversibility", "reversible"),
                ))
            except Exception:  # noqa: BLE001
                dropped += 1

        brief = Brief(
            case_id="",  # stamped by run_case from the trigger
            tldr=tldr,
            findings=list(state.findings),
            evidence=list(state.evidence),
            decision=Decision(
                action=action,  # type: ignore[arg-type]
                amount_minor=amount,
                currency=decision_currency or None,
                rationale=decision_rationale,
                confidence=conf,
            ),
            drafted_actions=safe_actions,
            hitl_question=hitl_question,
            generated_at=datetime.utcnow(),
        )
        state.brief = brief
        state.terminal = {"kind": "conclude", "dropped_actions": dropped}
        if tool_context is not None:
            tool_context.actions.escalate = True  # end the run cleanly
        return {
            "status": "ok",
            "finalised": True,
            "decision": action,
            "findings_count": len(state.findings),
            "actions_drafted": len(safe_actions),
            "actions_dropped": dropped,
            "note": "Investigation finalised. Do not call more tools — end now.",
        }

    return [
        coral_sql,
        coral_list_catalog,
        coral_describe_table,
        record_finding,
        ask_human,
        conclude,
    ]
