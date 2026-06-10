"""The investigation driver — ADK Runner inside, identical Event stream out.

This replaces the hand-rolled async-generator ReAct loop with Google ADK. The
public contract is unchanged on purpose: the investigator agent service
(`manthan-api/.../agents/investigator.py`) consumes this stream in-process and
persists it via `services/case_store.py`, and the script harnesses
(`agent/scripts/test_investigate.py`, `agent/scripts/smoke_adk.py`) assert on
the same shape:

    async for event in run_case(trigger, cfg, store):
        ...                                  # yields manthan_agent.types.Event
    # returns a Terminal via StopAsyncIteration.value

Internally we:
  1. read the live Coral MCP session the caller bound (set_active_coral_session)
  2. build the investigator TEAM via team.build_coordinator — the pro-model
     coordinator with the six adk_tools closures, the parallel specialist
     AgentTools (payments / customer-context / reliability / policy /
     network-rules), and the pacer callbacks, all sharing one RunState
  3. run it, translating each ADK event into the Manthan Event vocabulary
     (case_opened / tool_call / tool_result / finding_recorded / agent_thought
      / brief_drafted / hitl_pause / case_closed / error)

The Evidence list + integer citation indices + the defensive brief assembly all
live in adk_tools; the money-mover + pacing rules live in adk_pacer/pacer; the
team layout lives in team.py. The Event stream stays the contract for the test
harness, but production now writes through the case_store service directly.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .adk_tools import RunState
from .config import Config
from .coral_session import get_active_coral_session
from .state import EventStore
from .team import build_coordinator
from .tracing import current_trace_ids
from .types import CaseTrigger, Event

try:  # RunConfig location has moved across ADK versions; degrade gracefully.
    from google.adk.agents.run_config import RunConfig
except Exception:  # noqa: BLE001
    RunConfig = None  # type: ignore[assignment]

_APP_NAME = "manthan"
_USER_ID = "manthan-worker"
# Hard backstop above the pacer's round budget — prevents a runaway run if a
# callback ever fails to halt.
_MAX_LLM_CALLS = 60


@dataclass
class Terminal:
    """How the investigation ended (returned from the generator)."""

    reason: str  # "concluded" | "ask_human" | "budget" | "error"
    brief: Any | None = None
    question: str | None = None
    detail: str | None = None


async def _detect_catalog_tool(session: Any) -> str:
    """Coral 0.4.x exposes list_catalog; 0.3.x exposes list_tables."""
    try:
        listed = await session.list_tools()
        names = {t.name for t in listed.tools}
        return "list_catalog" if "list_catalog" in names else "list_tables"
    except Exception:  # noqa: BLE001
        return "list_tables"


def _render_trigger(trigger: CaseTrigger) -> str:
    lines = [
        f"case_id: {trigger.case_id}",
        f"source_surface: {trigger.source_surface}",
        "",
        trigger.text,
    ]
    if trigger.structured:
        lines += ["", "structured payload:", json.dumps(trigger.structured, indent=2, default=str)]
    return "<case_opened>\n" + "\n".join(lines) + "\n</case_opened>"


async def run_case(
    trigger: CaseTrigger,
    cfg: Config,
    store: EventStore | None = None,
    *,
    budget: Any | None = None,  # accepted for signature compat; ADK tracks usage
) -> AsyncGenerator[Event, Terminal]:
    """Investigate one case with an ADK agent; yield Events as they happen."""
    store = store or EventStore()
    cid = trigger.case_id

    def _append(kind: str, actor: str, data: dict[str, Any]) -> Event:
        # Stamp the current OTel trace/span ids on every event. ADK opens
        # spans around the run, so when an exporter is installed (tracing.
        # setup_tracing) these correlate the event log with Cloud Trace.
        # With no provider they're simply None — both are fine.
        trace_id, span_id = current_trace_ids()
        return store.append(cid, kind=kind, actor=actor, data=data,
                            trace_id=trace_id, span_id=span_id)

    # 0. Open the case.
    yield _append(
        kind="case_opened", actor="system",
        data={
            "case_id": cid,
            "text": trigger.text,
            "structured": trigger.structured,
            "source_surface": trigger.source_surface,
        },
    )

    # 1. The caller (worker / test) bound a live Coral MCP session.
    session = get_active_coral_session()
    if session is None:
        yield _append(kind="error", actor="system",
                      data={"reason": "no_coral_session"})
        yield _append(kind="case_closed", actor="system",
                      data={"reason": "error", "detail": "no active Coral session"})
        return

    # 2. Build the coordinator + specialists + pacer for this run. The team
    #    shares one RunState; the Gemini retry wrapper (transient AI Studio
    #    503s) lives inside team.gemini_with_retry.
    catalog_tool = await _detect_catalog_tool(session)
    state = RunState()
    agent = build_coordinator(cfg, session, catalog_tool, state, trigger.text)

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=_APP_NAME, user_id=_USER_ID, session_id=cid
    )
    runner = Runner(app_name=_APP_NAME, agent=agent, session_service=session_service)

    run_config = RunConfig(max_llm_calls=_MAX_LLM_CALLS) if RunConfig is not None else None
    new_message = types.Content(role="user", parts=[types.Part(text=_render_trigger(trigger))])

    # 3. Drive the agent. Translate each ADK event into Manthan Events.
    pending: dict[str, dict[str, Any]] = {}  # call_id -> {"name","args"}
    drained = 0

    def _drain_pacer() -> list[Event]:
        nonlocal drained
        out: list[Event] = []
        while drained < len(state.pacer_log):
            entry = state.pacer_log[drained]
            drained += 1
            out.append(_append(kind="agent_thought", actor="system", data=entry))
        return out

    try:
        kwargs: dict[str, Any] = dict(user_id=_USER_ID, session_id=cid, new_message=new_message)
        if run_config is not None:
            kwargs["run_config"] = run_config
        async for adk_event in runner.run_async(**kwargs):
            # Surface any pacer nudges/halts emitted during the model call.
            for e in _drain_pacer():
                yield e

            content = getattr(adk_event, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                fc = getattr(part, "function_call", None)
                fr = getattr(part, "function_response", None)
                text = getattr(part, "text", None)

                if fc is not None:
                    call_id = getattr(fc, "id", None) or f"call_{len(pending)}"
                    args = dict(getattr(fc, "args", None) or {})
                    pending[call_id] = {"name": fc.name, "args": args}
                    state.tool_calls.append((fc.name, args))
                    yield _append(kind="tool_call", actor="agent",
                                  data={"id": call_id, "name": fc.name, "arguments": args})

                elif fr is not None:
                    call_id = getattr(fr, "id", None) or ""
                    call = pending.get(call_id, {"name": getattr(fr, "name", ""), "args": {}})
                    result = getattr(fr, "response", None)
                    if not isinstance(result, dict):
                        result = {"result": result}
                    evidence_added = len(result.get("evidence_indices", []) or []) if isinstance(result, dict) else 0

                    # record_finding -> also emit a finding_recorded projection event.
                    if call["name"] == "record_finding" and result.get("status") == "ok":
                        a = call["args"]
                        yield _append(
                            kind="finding_recorded", actor="agent",
                            data={
                                "idx": result.get("finding_index"),
                                "text": a.get("text", ""),
                                "citations": a.get("citations", []),
                                "citations_resolved": result.get("citations_resolved", []),
                                "confidence": a.get("confidence"),
                            },
                        )

                    yield _append(kind="tool_result", actor="system",
                                  data={"tool_call_id": call_id, "result": result,
                                        "evidence_added": evidence_added})

                elif text and text.strip():
                    yield _append(kind="agent_thought", actor="agent",
                                  data={"text": text.strip()})
            # No early break: conclude/ask_human end the run via
            # tool_context.actions.escalate, and a pacer halt returns a final
            # LlmResponse — both let runner.run_async() finish in-context, so
            # ADK's OpenTelemetry spans tear down cleanly.
    except Exception as exc:  # noqa: BLE001
        yield _append(kind="error", actor="system",
                      data={"reason": "adk_run_failed", "detail": f"{type(exc).__name__}: {exc}"})
        yield _append(kind="case_closed", actor="system",
                      data={"reason": "error", "detail": f"{type(exc).__name__}: {exc}"})
        return

    for e in _drain_pacer():
        yield e

    # 4. Emit terminal events matching the old loop's vocabulary.
    term = state.terminal or {}
    kind = term.get("kind")

    if kind == "conclude" and state.brief is not None:
        state.brief.case_id = cid
        yield _append(kind="brief_drafted", actor="agent",
                      data=state.brief.model_dump(mode="json"))
        yield _append(kind="case_closed", actor="system",
                      data={"reason": "concluded"})
        return

    if kind == "ask_human":
        yield _append(kind="hitl_pause", actor="agent",
                      data={"reason": "ask_human", "question": term.get("question", ""),
                            "recommendation": term.get("recommendation", ""),
                            "confidence": term.get("confidence"),
                            "options": term.get("options", [])})
        yield _append(kind="case_closed", actor="system",
                      data={"reason": "ask_human", "question": term.get("question", "")})
        return

    if kind == "halt":
        yield _append(kind="case_closed", actor="system",
                      data={"reason": "pacer_halt", "detail": term.get("detail", "")})
        return

    # Agent stopped without concluding (ran to a natural end or hit the cap).
    yield _append(kind="case_closed", actor="system",
                  data={"reason": "error", "detail": "agent ended without conclude"})
    return
