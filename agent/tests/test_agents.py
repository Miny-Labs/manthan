"""Macro-agent constructor tests (agents.py) — NO LLM calls, $0.

make_triage_agent / make_advisor_agent are pure constructors; these tests
pin the model routing (triage=lite, advisor=flash), the tool surfaces
(triage has none; advisor gets exactly what the caller hands it), and the
key behavioral clauses in the instructions.
"""

from __future__ import annotations

from manthan_agent import config
from manthan_agent.agents import make_advisor_agent, make_triage_agent

_CFG = config.load()


def test_triage_agent_is_lite_and_toolless():
    agent = make_triage_agent(_CFG)
    assert agent.name == "triage"
    assert agent.model.model == _CFG.model_triage
    assert list(agent.tools) == []  # deterministic triage runs first; LLM only frames
    assert "trigger" in agent.instruction.lower()
    # the deterministic-first contract: the five known types never reach it
    assert "charge.dispute.created" in agent.instruction
    assert "NEVER drop an event" in agent.instruction


def test_advisor_agent_is_flash_conversational_readonly():
    def lookup_case(case_id: str) -> dict:
        """Test stand-in for a read-only case lookup tool."""
        return {"case_id": case_id}

    agent = make_advisor_agent(_CFG, [lookup_case])
    assert agent.name == "advisor"
    assert agent.model.model == _CFG.model_subagent
    assert [getattr(t, "__name__", None) for t in agent.tools] == ["lookup_case"]
    instr = agent.instruction
    assert "READ-ONLY" in instr
    assert "Cite" in instr or "cite" in instr
    assert "never issue refunds" in instr.lower()
