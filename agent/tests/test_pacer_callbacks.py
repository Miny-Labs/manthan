"""Unit tests for the pacer ADK callbacks — NO LLM calls, NO network, $0.

Verifies the pure pacer rules (pacer.py) are wired correctly into ADK's
callback surface: pre-round nudges inject context, the round-budget rule
halts (no findings) or wraps up (with findings), and the C1 money-mover gate
blocks a numeric refund until a finding shows the math.
"""

from __future__ import annotations

from google.adk.models import LlmResponse

from manthan_agent.adk_pacer import build_pacer_callbacks
from manthan_agent.adk_tools import RunState
from manthan_agent.types import Finding


class _FakeLlmRequest:
    def __init__(self) -> None:
        self.contents: list = []


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


def _finding(text: str) -> Finding:
    return Finding(text=text, citations=[0], confidence=0.9)


def test_halt_when_round_budget_exceeded_without_findings() -> None:
    state = RunState()
    before_model, _ = build_pacer_callbacks(state, "dispute du_1", max_rounds=2)
    req = _FakeLlmRequest()
    before_model(None, req)            # round 1
    before_model(None, req)            # round 2
    r3 = before_model(None, req)       # round 3 > budget, no findings -> halt
    assert isinstance(r3, LlmResponse)
    assert state.terminal is not None and state.terminal["kind"] == "halt"


def test_wrapup_when_budget_exceeded_with_findings() -> None:
    state = RunState()
    state.findings.append(_finding("some finding"))
    before_model, _ = build_pacer_callbacks(state, "dispute du_1", max_rounds=2)
    req = _FakeLlmRequest()
    before_model(None, req)
    before_model(None, req)
    r3 = before_model(None, req)       # budget exceeded but findings exist -> wrap_up (nudge)
    assert r3 is None
    assert any(e.get("pacer_rule_id", "").startswith("R5") for e in state.pacer_log)


def test_pre_round_nudge_injects_into_request() -> None:
    # stripe-shaped trigger, 3 rounds in, stripe.* never queried -> R1 nudge
    state = RunState()
    before_model, _ = build_pacer_callbacks(state, "chargeback du_1Tch1O ch_1", max_rounds=100)
    req = _FakeLlmRequest()
    before_model(None, req)
    before_model(None, req)
    before_model(None, req)            # round 3 -> R1
    assert any(e.get("pacer_rule_id") == "R1_stripe_unqueried" for e in state.pacer_log)
    assert len(req.contents) >= 1      # nudge injected into the model context


def test_conclude_gate_blocks_refund_without_math() -> None:
    state = RunState()
    state.findings.append(_finding("The customer disputed the charge."))
    _, before_tool = build_pacer_callbacks(state, "dispute du_1")
    res = before_tool(_FakeTool("conclude"),
                      {"decision_action": "refund", "decision_amount_minor": 56000}, None)
    assert isinstance(res, dict) and res["status"] == "rejected"


def test_conclude_gate_allows_refund_with_math() -> None:
    state = RunState()
    state.findings.append(_finding("Credit = 2/30 x $8,400 = $560 (pro-rata)."))
    _, before_tool = build_pacer_callbacks(state, "dispute du_1")
    res = before_tool(_FakeTool("conclude"),
                      {"decision_action": "refund", "decision_amount_minor": 56000}, None)
    assert res is None     # math present -> allowed through


def test_conclude_gate_ignores_non_conclude_and_nonrefund() -> None:
    state = RunState()
    _, before_tool = build_pacer_callbacks(state, "dispute du_1")
    assert before_tool(_FakeTool("coral_sql"), {"query": "SELECT 1"}, None) is None
    assert before_tool(_FakeTool("conclude"),
                       {"decision_action": "fight", "decision_amount_minor": 0}, None) is None
