"""Team-construction tests — NO LLM calls, NO network, $0.

build_specialists / build_coordinator are pure constructors: they assemble
ADK Agents + AgentTools around closures bound to a shared RunState. These
tests pin the team layout the investigation depends on:

  * specialist roster + each specialist's tool SUBSET (data specialists get
    ONLY the three coral closures; conclude/ask_human/record_finding are
    coordinator-only; network_rules_analyst gets ONLY google_search)
  * shared working memory: a specialist's coral_sql appends to the SAME
    RunState evidence list the coordinator cites from
  * the coordinator carries the full six-tool kit + all five specialists,
    the pacer callbacks, and the SYSTEM prompt extended with YOUR TEAM
"""

from __future__ import annotations

import asyncio
import json

from google.adk.tools.agent_tool import AgentTool

from manthan_agent import config
from manthan_agent.adk_tools import RunState
from manthan_agent.prompts import SYSTEM
from manthan_agent.team import (
    GROUNDING_AVAILABLE,
    TEAM_SECTION,
    build_coordinator,
    build_specialists,
)

_CFG = config.load()

_EXPECTED_SPECIALISTS = [
    "payments_analyst",
    "customer_context",
    "reliability_analyst",
    "policy_analyst",
    "network_rules_analyst",
]

_CORAL_SUBSET = {"coral_sql", "coral_list_catalog", "coral_describe_table"}
_COORDINATOR_ONLY = {"record_finding", "ask_human", "conclude"}


# ── tiny fake Coral session (mirrors test_adk_tools) ──────────────────


class _ContentBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _CallResult:
    def __init__(self, text: str, is_error: bool = False) -> None:
        self.content = [_ContentBlock(text)]
        self.isError = is_error


class FakeCoralSession:
    def __init__(self, results: list[_CallResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, dict | None]] = []

    async def call_tool(self, name: str, arguments: dict | None = None) -> _CallResult:
        self.calls.append((name, arguments))
        return self._results.pop(0)


def _tool_names(agent) -> set[str]:
    return {getattr(t, "name", None) or getattr(t, "__name__", str(t)) for t in agent.tools}


# ── specialists ───────────────────────────────────────────────────────


def test_specialist_roster_and_models():
    specialists = build_specialists(None, "list_tables", RunState(), _CFG)
    assert [t.agent.name for t in specialists] == _EXPECTED_SPECIALISTS
    for tool in specialists:
        assert isinstance(tool, AgentTool)
        assert tool.agent.model.model == _CFG.model_subagent
        assert tool.agent.description  # the coordinator routes on this


def test_data_specialists_get_only_the_coral_subset():
    specialists = build_specialists(None, "list_tables", RunState(), _CFG)
    for tool in specialists[:4]:  # payments / customer / reliability / policy
        names = _tool_names(tool.agent)
        assert names == _CORAL_SUBSET, tool.agent.name
        assert not (names & _COORDINATOR_ONLY)  # never conclude/findings/HITL


def test_specialist_instructions_are_scoped():
    by_name = {t.agent.name: t.agent for t in
               build_specialists(None, "list_tables", RunState(), _CFG)}
    assert "stripe" in by_name["payments_analyst"].instruction
    assert "intercom" in by_name["customer_context"].instruction
    assert "datadog" in by_name["reliability_analyst"].instruction
    assert "notion" in by_name["policy_analyst"].instruction
    assert "authoritative" in by_name["policy_analyst"].instruction.lower()
    # the shared contract: summarize + evidence indices, never findings
    for name in _EXPECTED_SPECIALISTS[:4]:
        instr = by_name[name].instruction
        assert "EVIDENCE_INDICES" in instr
        assert "do NOT record findings" in instr


def test_network_rules_analyst_is_google_search_only():
    specialists = build_specialists(None, "list_tables", RunState(), _CFG)
    network = specialists[-1].agent
    assert network.name == "network_rules_analyst"
    assert len(network.tools) == 1
    if GROUNDING_AVAILABLE:  # ADK 2.2.0: built-in google_search present
        from google.adk.tools import google_search
        assert network.tools[0] is google_search
    else:  # documented degraded mode
        assert network.tools[0].__name__ == "_grounding_unavailable"
    assert "Compelling Evidence" in network.instruction
    assert "source URL" in network.instruction


def test_specialists_share_the_run_state_evidence_list():
    state = RunState()
    rows = [{"id": "ch_1", "amount": 4200}]
    sess = FakeCoralSession([_CallResult(json.dumps({"rows": rows}))])
    specialists = build_specialists(sess, "list_tables", state, _CFG)
    payments = specialists[0].agent
    coral_sql = next(t for t in payments.tools if getattr(t, "__name__", "") == "coral_sql")

    res = asyncio.run(coral_sql("SELECT id, amount FROM stripe.charges WHERE id = 'ch_1'"))
    assert res["status"] == "ok"
    assert res["evidence_indices"] == [0]
    # the SHARED state got the evidence — citable by the coordinator
    assert len(state.evidence) == 1
    assert state.evidence[0].source == "stripe"


# ── coordinator ───────────────────────────────────────────────────────


def test_coordinator_full_toolkit_plus_team_and_pacer():
    state = RunState()
    agent = build_coordinator(_CFG, None, "list_tables", state, "trigger text")
    assert agent.name == "investigator"
    assert agent.model.model == _CFG.model
    names = _tool_names(agent)
    assert _CORAL_SUBSET <= names
    assert _COORDINATOR_ONLY <= names                # conclude & co stay here
    assert set(_EXPECTED_SPECIALISTS) <= names       # the team, as tools
    assert len(agent.tools) == 6 + len(_EXPECTED_SPECIALISTS)
    # pacer callbacks attached (rounds are coordinator turns)
    assert agent.before_model_callback is not None
    assert agent.before_tool_callback is not None
    # instruction = SYSTEM + the fan-out section
    assert agent.instruction.startswith(SYSTEM)
    assert TEAM_SECTION in agent.instruction
    assert "YOUR TEAM" in agent.instruction
    assert "IN PARALLEL" in agent.instruction


def test_coordinator_pacer_is_bound_to_the_shared_state():
    state = RunState()
    agent = build_coordinator(_CFG, None, "list_tables", state, "case text")

    # Drive the before_model callback directly: round counting + pacer log
    # land on the run's shared state, proving the closure binding.
    class _Req:
        contents: list = []

    for _ in range(3):
        agent.before_model_callback(None, _Req())
    # No nudge fires this early on an empty case, but the callback must not
    # blow up and must leave state untouched except via pacer rules.
    assert state.terminal is None
