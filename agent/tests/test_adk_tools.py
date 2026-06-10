"""Unit tests for the ADK tool closures — NO LLM calls, NO network, $0.

These cover the deterministic logic the agent's correctness rests on:
Evidence-indexed citations, the defensive conclude() parsing/clamping, and
the escalate/HITL signalling. Coral is never touched (record_finding /
conclude / ask_human don't call it), so coral_session can be None.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from manthan_agent.adk_tools import RunState, build_tools
from manthan_agent.types import Evidence


class _FakeActions:
    def __init__(self) -> None:
        self.escalate = False


class _FakeToolContext:
    def __init__(self) -> None:
        self.actions = _FakeActions()


class _ContentBlock:
    """Mimics one MCP text content block (has .text)."""

    def __init__(self, text: str) -> None:
        self.text = text


class _CallResult:
    """Mimics an MCP CallToolResult (.content list + .isError)."""

    def __init__(self, text: str, is_error: bool = False) -> None:
        self.content = [_ContentBlock(text)]
        self.isError = is_error


class FakeCoralSession:
    """Stands in for a live Coral MCP ClientSession — canned results, no I/O."""

    def __init__(self, results: list[_CallResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, dict | None]] = []

    async def call_tool(self, name: str, arguments: dict | None = None) -> _CallResult:
        self.calls.append((name, arguments))
        return self._results.pop(0)


def _tools(state: RunState, session: FakeCoralSession | None = None) -> dict:
    return {f.__name__: f for f in build_tools(session, "list_tables", state)}


def _ev(state: RunState, source: str, rec: str) -> None:
    state.evidence.append(Evidence(
        source=source, table="t", record_id=rec, fields={}, query="q",
        retrieved_at=datetime.utcnow(),
    ))


def test_coral_sql_happy_path_appends_evidence() -> None:
    state = RunState()
    rows = [{"id": "du_1", "amount": 840000}]
    sess = FakeCoralSession([_CallResult(json.dumps({"rows": rows}))])
    res = asyncio.run(_tools(state, sess)["coral_sql"](
        "SELECT d.id, d.amount FROM stripe.disputes d WHERE d.id = 'du_1'"))
    assert res["status"] == "ok"
    assert res["rows"] == rows
    assert res["row_count"] == 1
    assert res["evidence_indices"] == [0]          # citable from record_finding
    assert len(state.evidence) == 1
    assert state.evidence[0].source == "stripe"    # tagged from the query's schema ref
    assert state.evidence[0].fields["row_count"] == 1
    assert sess.calls[0][0] == "sql"


def test_coral_sql_error_path_records_coral_error_evidence() -> None:
    state = RunState()
    sess = FakeCoralSession(
        [_CallResult('relation "stripe.nope" does not exist', is_error=True)])
    res = asyncio.run(_tools(state, sess)["coral_sql"]("SELECT * FROM stripe.nope"))
    assert res["status"] == "error"
    assert "does not exist" in res["error"]
    assert res["evidence_indices"] == [0]          # the error itself is evidence
    assert len(state.evidence) == 1
    assert state.evidence[0].source == "coral_error"
    assert state.evidence[0].fields["sql_error"] == 'relation "stripe.nope" does not exist'


def test_record_finding_resolves_citations() -> None:
    state = RunState()
    _ev(state, "stripe", "du_1")
    res = asyncio.run(_tools(state)["record_finding"](
        text="The dispute is $8,400. (du_1)", citations=[0], confidence=0.9))
    assert res["status"] == "ok"
    assert len(state.findings) == 1
    assert res["citations_resolved"][0]["source"] == "stripe"
    assert res["citations_resolved"][0]["ref"] == "du_1"


def test_record_finding_rejects_missing_citation() -> None:
    state = RunState()
    res = asyncio.run(_tools(state)["record_finding"](text="claim", citations=[], confidence=0.9))
    assert res["status"] == "error"
    assert state.findings == []


def test_record_finding_clamps_confidence() -> None:
    state = RunState()
    _ev(state, "notion", "p_1")
    res = asyncio.run(_tools(state)["record_finding"](text="x (p_1)", citations=[0], confidence=5.0))
    assert state.findings[0].confidence == 1.0
    assert res["status"] == "ok"


def test_conclude_builds_brief_drops_bad_actions_and_clamps() -> None:
    state = RunState()
    ctx = _FakeToolContext()
    actions = (
        '[{"kind":"stripe_refund","payload":{"charge":"ch_1","amount_minor":56000},'
        '"description":"refund $560"},{"kind":"NOT_A_KIND","payload":{}}]'
    )
    res = asyncio.run(_tools(state)["conclude"](
        tldr="We recommend a $560 partial refund.",
        decision_action="refund",
        decision_rationale="pro-rata math [1]",
        decision_confidence=1.5,            # -> clamped to 1.0
        hitl_question="Concur?",
        decision_amount_minor=56000,
        decision_currency="usd",
        drafted_actions_json=actions,
        tool_context=ctx,
    ))
    assert res["status"] == "ok"
    assert state.brief is not None
    assert state.brief.decision.action == "refund"
    assert state.brief.decision.confidence == 1.0
    assert state.brief.decision.amount_minor == 56000
    assert [a.kind for a in state.brief.drafted_actions] == ["stripe_refund"]  # bad one dropped
    assert res["actions_dropped"] == 1
    assert ctx.actions.escalate is True            # ends the ADK run cleanly
    assert state.terminal["kind"] == "conclude"


def test_conclude_invalid_action_becomes_escalate() -> None:
    state = RunState()
    asyncio.run(_tools(state)["conclude"](
        tldr="x", decision_action="banana", decision_rationale="y",
        decision_confidence=0.5, hitl_question="?"))
    assert state.brief.decision.action == "escalate"


def test_conclude_handles_malformed_actions_json() -> None:
    state = RunState()
    res = asyncio.run(_tools(state)["conclude"](
        tldr="x", decision_action="fight", decision_rationale="y",
        decision_confidence=0.8, hitl_question="?", drafted_actions_json="not json{"))
    assert res["status"] == "ok"
    assert state.brief.drafted_actions == []


def test_ask_human_sets_terminal_and_escalate() -> None:
    state = RunState()
    ctx = _FakeToolContext()
    res = _tools(state)["ask_human"](
        question="Which way?", recommendation="refund", confidence=0.6,
        options=["a", "b"], tool_context=ctx)
    assert res["status"] == "ok"
    assert ctx.actions.escalate is True
    assert state.terminal["kind"] == "ask_human"
    assert state.terminal["question"] == "Which way?"


def test_ask_human_without_tool_context_still_sets_terminal() -> None:
    state = RunState()
    res = _tools(state)["ask_human"](
        question="Refund or fight?", recommendation="refund", confidence=0.55,
        tool_context=None)
    assert res["status"] == "ok"
    assert state.terminal["kind"] == "ask_human"
    assert state.terminal["question"] == "Refund or fight?"
    assert state.terminal["options"] == []
