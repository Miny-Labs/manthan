# `agent/` — the Manthan investigation loop

This directory is the brain. Everything else in Manthan (the FastAPI
in `manthan-api/`, the React surface in `manthan-ui/`, the three
worker processes) exists to deliver cases to this loop and to act on
what it decides. The loop itself is a few hundred lines of Python.
No agent framework. No orchestrator. Just an async generator that
yields typed `Event` objects until the case is resolved.

If you are reading the codebase for the first time, start here.

## The thirty-second mental model

A case arrives. The loop opens it, sends a system prompt + the case
text + the running event log to an LLM, and then dispatches whatever
tool the model wants to call next. The dispatch lives in
`tools.py`. Most tool calls are `coral_sql` (read every connected
source through the Coral MCP server); a few are write-side tools the
loop itself implements (`record_finding`, `amend_brief`,
`conclude`). The result of every call becomes another event in the
log. The next LLM turn sees the whole log. We do this until the
model emits `conclude`, at which point the loop yields a `Brief` and
returns.

Every fact the loop asserts is cited back to a source row. The agent
never holds write credentials. The actual mutations (Stripe refunds,
HubSpot notes, Slack posts, emails) are queued as `DraftedAction`s
in the brief and executed by a separate process after a human (or a
policy rule) approves them.

## The files

| File | What lives there |
|---|---|
| `loop.py` | The driver. `run_case(trigger, cfg)` builds a [Google ADK](https://google.github.io/adk-docs/) Agent (Gemini via AI Studio, retry/backoff on 503s), runs it, and translates ADK events into the same `Event` stream the worker has always consumed. |
| `types.py` | `CaseTrigger`, `Evidence`, `Finding`, `Decision`, `DraftedAction`, `Brief`, `Event`, `NextStep` union. The locked vocabulary. Read this file second. |
| `adk_tools.py` | The six investigator tools as ADK function tools, built per-run as closures over a `RunState` (Evidence list + integer citations + the final Brief). Coral tools dispatch through the MCP stdio session; `conclude`/`ask_human` end the run via `tool_context.actions.escalate`. |
| `adk_pacer.py` | The pacer wired as ADK callbacks: pre-round rules (R1–R6) as `before_model_callback`, the C1 refund-math money-mover gate as `before_tool_callback` on `conclude`. |
| `triage.py` | Pure Stripe-event router: `trigger_from_stripe_event()` maps the 5 webhook event types to investigation triggers. |
| `tracing.py` | OpenTelemetry setup: Cloud Trace exporter when `GOOGLE_CLOUD_PROJECT` is set (install the `gcp` extra), OTLP fallback, silent no-op otherwise. Trace/span ids are stamped on every event. |
| `a2a/` | The A2A surface: Agent Card builder, JSON-RPC dispatch (`message/send`, `tasks/get`), and the `CaseStore` protocol — every case artifact is pickup-able by external agents. |
| `coral_session.py` | The MCP stdio session to the Coral binary. Bound per-run via `set_active_coral_session()` so tool calls dispatch through the live session without threading the handle through every signature. |
| `state.py` | `EventStore` (in-memory append-only log) and the function that converts `Event`s to OpenAI chat messages for the next turn. |
| `pacer.py` | Pre-round and pre-conclude judges. Bounded LLM calls that decide whether the agent has enough to conclude or should run another round. Keeps the loop from spinning. |
| `prompts.py` | `SYSTEM` and `REFLEXION` prompts. Editable as plain text; no templating magic. |
| `llm.py` | google-genai helpers for Gemini via AI Studio: `generate_text` / `agenerate_text` for one-shot calls, plus an OpenAI-compat `chat()` shim that keeps the operator-chat worker unchanged. |
| `config.py` | `Config` dataclass plus `load()` from environment. Read once at process start. |

## How a case actually runs

```text
investigate worker (manthan-api/workers/investigate.py)
       │
       │  picks up a `cases` row from a PG LISTEN/NOTIFY signal
       ▼
   builds a CaseTrigger
       │
       │  async for evt in run_case(trigger, cfg):
       ▼                       │
   ┌───────────────┐            │
   │   agent loop  │   yields:  │
   │   (loop.py)   │   case_opened
   │               │   tool_call
   │   each turn:  │   tool_result
   │   1. judge    │   finding_recorded
   │      pre-round│   reflexion
   │   2. LLM call │   decision_recorded
   │   3. dispatch │   draft_action_added
   │      tools    │   brief_drafted     ← terminal
   │   4. append   │   case_closed
   │      events   │
   │   5. reflexion│
   │      every 3  │
   │      turns    │
   └───────────────┘
       │
   investigate worker
       │
       │  persists each event to PG events table,
       │  projects findings → findings table,
       │  projects drafted_actions → actions table
       ▼
   actor worker  (after human or policy approval)
       │
       │  drains the actions queue, calls Stripe / Resend /
       │  HubSpot / Slack adapters, marks each succeeded/failed.
       ▼
   case status → resolved
```

The agent itself does not know any of the persistence happened. It
only sees the event log it is yielding into.

## Why ADK (and what we kept from the no-framework era)

The first version of this agent was a hand-rolled async-generator
loop — no framework, one readable file. The Track 3 rebuild moved
it onto [Google ADK](https://google.github.io/adk-docs/) because the
things we'd otherwise re-implement (Gemini function-calling plumbing,
A2A interop, OpenTelemetry spans around every model/tool call, an
eval harness) come built in.

What survived the port unchanged is the part that matters: the typed
event log as the single source of truth (`run_case` still yields the
exact same `Event` stream, so the worker never noticed the swap), the
Evidence list with integer citations, and the pacer — its pure rules
now fire from ADK callbacks instead of inline checks.

## Tool surface (what the LLM can actually call)

Read tools (parallel-safe, dispatched through Coral MCP):

- `coral_list_catalog` — list every connected source + their tables.
- `coral_describe_table` — column types for one qualified table.
- `coral_sql` — execute a SELECT across any connected source.

Write tools (handled inside the loop, no external side effects):

- `record_finding(text, citations[idx], confidence)` — assert a
  cited claim. Joins the running findings list. The brief is built
  from these.
- `amend_brief(reason, decision_*?, regenerate_actions?)` — used by
  the chat-followup phase to revise a drafted brief after new
  evidence.
- `ask_human(question, recommendation, options[], confidence)` —
  terminates the loop with a `NextStepInterruption`. The case sits
  in `awaiting_approval` until a human responds.
- `conclude(tldr, decision, drafted_actions[])` — terminates the
  loop with a `NextStepFinalOutput`. The brief is yielded as
  `brief_drafted` and the worker drains drafted_actions into the
  PG actions table.

## Running it standalone

The agent runs inside the `manthan-investigate` worker in
production, but you can drive it from a script for debugging.

```bash
cd agent
uv venv && uv pip install -e .

# Point at AI Studio + the Coral binary.
cp .env.example .env
$EDITOR .env   # set GOOGLE_API_KEY (aistudio.google.com/apikey); CORAL_BINARY defaults to `coral` on PATH

# Run a single case end-to-end against a local Coral.
uv run python -m manthan_agent.smoke aperture
```

The smoke script lives in `scripts/`. There are also `seed_*.py`
scripts in `scripts/` that populate the Coral test database with
the customers, charges, disputes, and CRM records that the demo
scenarios reference. Run them once after standing up a fresh
Coral instance.

## What's NOT in this directory

- **The actor that fires actions.** That lives in
  `manthan-api/workers/actor.py`. The agent never holds write
  credentials.
- **The HTTP surface.** That lives in `manthan-api/`. The agent
  doesn't speak HTTP at all; it speaks events.
- **Per-tenant config.** The agent only knows about the case it was
  handed. Org resolution, member auth, and policy matching all
  happen upstream.
- **Coral itself.** Coral is a separate Rust binary the agent talks
  to over MCP stdio. The agent has no idea what's behind that
  socket — could be the production Coral pointed at fifteen real
  sources, could be a local Coral pointed at a SQLite test database.

## Editing notes

- Adding a tool: add a function closure in `adk_tools.build_tools()`
  (Google-style docstring becomes the declaration) and return it in
  the list. ADK picks it up — no registration step.
- Changing the system prompt: edit `prompts.py`. There is no
  templating. The trigger text is appended verbatim by `loop.py`.
- Changing the model: `MANTHAN_MODEL=...` in the env. Default is
  `gemini-3.1-pro-preview`; sub-agents run `MANTHAN_MODEL_SUBAGENT`
  (default `gemini-3.5-flash`), triage/prettifier run
  `MANTHAN_MODEL_TRIAGE` (default `gemini-3.1-flash-lite`). All via
  AI Studio with `GOOGLE_API_KEY`.
- Changing the round budget: `_MAX_LLM_CALLS` in `loop.py` is the
  hard backstop; the pacer's `max_rounds` governs normal wrap-up.

## Reading order if you have ten minutes

1. `types.py` — the vocabulary.
2. `loop.py` — the main generator. Read top to bottom.
3. `tools.py` — what the LLM can call.
4. `prompts.py` — what the LLM is told to do.

Skip `pacer.py`, `state.py`, `llm.py`, `coral_session.py`,
`config.py` until you need them. They are mechanical.
