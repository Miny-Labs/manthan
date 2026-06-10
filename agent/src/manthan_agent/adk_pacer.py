"""The pacer, wired as ADK callbacks.

`pacer.py` holds the pure, unit-tested rules (R1-R6 pre-round, C1 pre-conclude).
This module adapts them to ADK's callback surface without changing a rule:

  * before_model_callback  — runs the pre-round rules each turn. Nudges/wrap-up
    are injected into the model's context for the next turn; a round-budget
    halt short-circuits the model call with a final response (ends the run).
  * before_tool_callback   — gates `conclude`. The C1 money-mover invariant
    refuses to finalise a numeric refund unless a finding shows the math;
    on a miss it substitutes a "rejected" tool result so the agent records
    the math and tries again, instead of writing a brief with a hallucinated
    number.

Both callbacks are built per-run as closures over the run's RunState so the
snapshot the rules read (rounds, findings, tool history, fired nudges) stays
consistent and concurrent cases never share pacer state.
"""

from __future__ import annotations

from typing import Any

from google.adk.models import LlmResponse
from google.genai import types

from .adk_tools import RunState
from .pacer import CaseSnapshot, judge_pre_conclude, judge_pre_round


def build_pacer_callbacks(
    state: RunState, trigger_text: str, *, max_rounds: int = 100
):
    """Return (before_model_callback, before_tool_callback) for this run."""
    counter = {"rounds": 0}
    nudges_fired: set[str] = set()

    def _snapshot() -> CaseSnapshot:
        return CaseSnapshot(
            round_count=counter["rounds"],
            findings_count=len(state.findings),
            findings_text=[f.text for f in state.findings],
            tool_calls=list(state.tool_calls),
            nudges_fired=set(nudges_fired),
            trigger_text=trigger_text,
        )

    def before_model(callback_context: Any, llm_request: Any) -> LlmResponse | None:
        counter["rounds"] += 1
        pace = judge_pre_round(_snapshot(), max_rounds=max_rounds)

        if pace.kind in ("nudge", "wrap_up"):
            nudges_fired.add(pace.rule_id)
            state.pacer_log.append({"text": pace.message, "pacer_rule_id": pace.rule_id})
            # Surface the nudge to the model as an extra user turn this round.
            try:
                llm_request.contents.append(
                    types.Content(role="user", parts=[types.Part(text=pace.message)])
                )
            except Exception:  # noqa: BLE001 — never let a nudge break the call
                pass
            return None

        if pace.kind == "halt":
            nudges_fired.add(pace.rule_id)
            state.pacer_log.append({"text": pace.message, "pacer_rule_id": pace.rule_id})
            state.terminal = {
                "kind": "halt",
                "detail": pace.reason,
                "message": pace.message,
            }
            # Returning an LlmResponse short-circuits the model call; with no
            # tool calls in it, the agent ends its turn — the run stops.
            return LlmResponse(
                content=types.Content(
                    role="model", parts=[types.Part(text=pace.message)]
                )
            )
        return None

    def before_tool(tool: Any, args: dict, tool_context: Any) -> dict | None:
        if getattr(tool, "name", "") != "conclude":
            return None
        conclude_args = {
            "decision_action": args.get("decision_action", ""),
            "decision_amount_minor": args.get("decision_amount_minor", 0),
        }
        pace = judge_pre_conclude(_snapshot(), conclude_args)
        if pace.kind == "nudge":
            nudges_fired.add(pace.rule_id)
            state.pacer_log.append({"text": pace.message, "pacer_rule_id": pace.rule_id})
            # Substitute the tool result: conclude does NOT run this turn.
            return {
                "status": "rejected",
                "reason": pace.message,
                "hint": (
                    "Record a finding containing the arithmetic that produced "
                    "the refund amount, then call conclude again."
                ),
            }
        return None

    return before_model, before_tool
