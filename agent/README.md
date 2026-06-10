# `agent/` — the Manthan investigation team

This directory is the brain. Everything else in Manthan (the FastAPI +
three A2A agent services in `manthan-api/`, the React surface in
`manthan-ui/`, the two deterministic workers) exists to deliver cases
to this code and to act on what it decides. The agent is built on
[Google ADK](https://google.github.io/adk-docs/): a pro-model
coordinator that fans out five parallel flash specialists, all sharing
one Evidence set, driven through a single async generator —
`run_case()` — that yields typed `Event` objects until the case is
resolved.

If you are reading the codebase for the first time, start here.

## The thirty-second mental model

A case arrives. `run_case()` (in `loop.py`) builds the investigator
team via `team.build_coordinator()`: a coordinator Agent holding the
six investigation tools (built per-run in `adk_tools.py`) plus five
specialist sub-agents wrapped as AgentTools (`team.py`). The
coordinator fans the specialists out in parallel; each one runs
`coral_sql` reads through the Coral MCP session and appends rows to
the SAME Evidence list, then returns a summary naming the evidence
indices it added. The coordinator synthesizes `record_finding` calls
with integer citations into that list and finishes with `conclude`
(or `ask_human`). Every ADK event is translated into the Manthan
`Event` vocabulary and yielded; the run ends with a `Brief`.

Every fact the team asserts is cited back to a source row. The agent
never holds write credentials. The actual mutations (Stripe refunds,
HubSpot notes, Slack posts, emails) are queued as `DraftedAction`s
in the brief and executed by a separate process after a human (or a
policy rule) approves them.

## Where this package sits (macro topology)

Three A2A macro agents make up the system; they live as services in
`manthan-api/src/manthan_api/agents/` and are all built from this
package:

```text
Stripe webhook (5 event types)
      │
      ▼
  triage agent  (gemini-3.1-flash-lite; agents.py + triage.py)
      │  A2A `investigate_dispute` (in-process local fallback)
      ▼
  investigator agent  (gemini-3.1-pro-preview coordinator, team.py)
      │  runs run_case() in-process; 5 parallel flash specialists:
      │  payments_analyst / customer_context / reliability_analyst /
      │  policy_analyst (policy-docs RAG) / network_rules_analyst
      │  (google_search grounding); writes events + projections
      │  itself via manthan-api services/case_store.py
      ▼
  advisor agent  (gemini-3.5-flash; A2A skills ask / precheck_refund /
      get_customer_history / dispute_exposure / contribute_evidence
      + 6 reads; answers operator per-case chat)
```

A2A is the boundary BETWEEN macro agents. Inside the investigator,
everything is in-process so evidence indices stay coherent and
citation chips keep working.

## The files

| File | What lives there |
|---|---|
| `loop.py` | The driver. `run_case(trigger, cfg)` builds the investigator team (Gemini via AI Studio, retry/backoff on 503s), runs it under an ADK Runner, and translates ADK events into the typed `Event` stream every consumer reads. |
| `types.py` | `CaseTrigger`, `Evidence`, `Finding`, `Decision`, `DraftedAction`, `Brief`, `Event`, `NextStep` union. The locked vocabulary. Read this file second. |
| `team.py` | The investigator TEAM: `build_specialists()` (the five flash sub-agents wrapped as AgentTools — `ResilientAgentTool` caps each at 180s and degrades to an apology string instead of crashing the run) and `build_coordinator()` (the pro-model Agent with the six tools + specialists + pacer callbacks). |
| `agents.py` | The A2A-visible macro-agent constructors: triage, investigator (delegates to `team.py`), advisor. Pure functions from `Config` to ADK `Agent` — no network at build time. |
| `adk_tools.py` | The six investigator tools as ADK function tools, built per-run as closures over a `RunState` (Evidence list + integer citations + the final Brief). Coral tools dispatch through the MCP stdio session; `conclude`/`ask_human` end the run via `tool_context.actions.escalate`. |
| `adk_pacer.py` | The pacer wired as ADK callbacks: pre-round rules (R1–R6) as `before_model_callback`, the C1 refund-math money-mover gate as `before_tool_callback` on `conclude`. |
| `pacer.py` | The pure pacing rules themselves (round budget, repeat-query, thin-findings, refund-math). `adk_pacer.py` fires them from callbacks. |
| `triage.py` | Pure Stripe-event router: `trigger_from_stripe_event()` maps the 5 webhook event types to investigation triggers. |
| `tracing.py` | OpenTelemetry setup: Cloud Trace exporter when `GOOGLE_CLOUD_PROJECT` is set (install the `gcp` extra), OTLP fallback, silent no-op otherwise. Trace/span ids are stamped on every event. |
| `a2a/` | The A2A surface: Agent Card builder (`card.py`), JSON-RPC dispatch (`server.py`: `message/send`, `tasks/get`), a typed client (`client.py`) for agent-to-agent calls, and the `CaseStore` protocol (`store.py`) — every case artifact is pickup-able by external agents. |
| `coral_session.py` | The MCP stdio session to the Coral binary. Bound per-run via `set_active_coral_session()` so tool calls dispatch through the live session without threading the handle through every signature. |
| `state.py` | `EventStore`, the in-memory append-only event log used by `run_case` and the script harnesses. Production persistence happens in `manthan-api`'s `services/case_store.py`. |
| `prompts.py` | The `SYSTEM` prompt (persona, Coral usage, output contract). `team.py` extends it with the specialist fan-out section. Editable as plain text; no templating magic. |
| `llm.py` | google-genai helpers for Gemini via AI Studio: `generate_text` / `agenerate_text` for the one-shot non-agent calls (event prettifier, cross-case chat summariser). |
| `config.py` | `Config` dataclass plus `load()` from environment. Read once at process start. |

## How a case actually runs

```text
triage agent (manthan-api/agents/triage.py service)
       │
       │  Stripe webhook → trigger_from_stripe_event() → A2A
       │  `investigate_dispute` (in-process fallback when the
       │  investigator service isn't reachable)
       ▼
investigator agent (manthan-api/agents/investigator.py service)
       │
       │  async for evt in run_case(trigger, cfg):
       ▼                       │
   ┌────────────────────┐      │
   │  coordinator       │      │  yields:
   │  (team.py, pro)    │      │  case_opened
   │    │ fan-out       │      │  tool_call
   │    ▼ (parallel)    │      │  tool_result
   │  5 specialists     │      │  finding_recorded
   │  (flash) sharing   │      │  agent_thought
   │  ONE Evidence set  │      │  brief_drafted   ← terminal
   │    │               │      │  (or hitl_pause,
   │  record_finding /  │      │   or error)
   │  conclude on the   │      │  case_closed
   │  coordinator only  │      │
   └────────────────────┘      │
       │
       │  the investigator service persists each event to PG and
       │  projects findings → findings table, drafted_actions →
       │  actions table, via manthan-api services/case_store.py
       ▼
actor worker  (manthan-api/workers/actor.py, after human or
       │       policy approval — auto under $50, one-click
       │       $50–500, two-person $500+)
       │
       │  drains the actions queue, calls Stripe / Resend /
       │  HubSpot / Slack adapters, marks each succeeded/failed.
       ▼
case status → resolved      (advisor agent answers operator
                             questions about the case over A2A)
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
exact same `Event` stream), the Evidence list with integer citations,
and the pacer — its pure rules now fire from ADK callbacks instead of
inline checks. What's new is the team: one agent with six tools became
a coordinator with five parallel specialists, which is what makes a
fifteen-source investigation finish in operator time.

## Tool surface (what the coordinator can actually call)

Read tools (parallel-safe, dispatched through Coral MCP — the
specialists use these too):

- `coral_list_catalog` — list every connected source + their tables.
- `coral_describe_table` — column types for one qualified table.
- `coral_sql` — execute a SELECT across any connected source.

Specialist fan-out (coordinator only — each is an AgentTool):

- `payments_analyst` — stripe.* (charges, disputes, invoices, refunds).
- `customer_context` — CRM + support (salesforce/hubspot, intercom).
- `reliability_analyst` — datadog / sentry / pagerduty / posthog.
- `policy_analyst` — merchant policy retrieval, wherever the docs live (Notion, Confluence, Google Docs — whichever schemas the catalog shows); the RAG surface.
- `network_rules_analyst` — ADK built-in `google_search` grounding for
  card-network rules (its own agent: built-in tools can't be mixed
  with function tools).

Write tools (handled inside the run, no external side effects;
coordinator only):

- `record_finding(text, citations[idx], confidence)` — assert a
  cited claim. Joins the running findings list. The brief is built
  from these.
- `ask_human(question, recommendation, options[], confidence)` —
  ends the run with a `hitl_pause`. The case sits in
  `awaiting_approval` until a human responds.
- `conclude(tldr, decision, drafted_actions[])` — ends the run with
  the final brief, yielded as `brief_drafted`; the investigator
  service projects drafted_actions into the PG actions table.

## Running it standalone

The agent runs inside the investigator agent service
(`manthan-api/src/manthan_api/agents/investigator.py`) in production,
but you can drive it from the script harnesses for debugging.

```bash
cd agent
uv venv && uv pip install -e .

# Point at AI Studio + the Coral binary.
cp .env.example .env
$EDITOR .env   # set GOOGLE_API_KEY (aistudio.google.com/apikey); CORAL_BINARY defaults to `coral` on PATH

# Foundation smoke: AI Studio reachable, ADK runs a tool, Coral answers over MCP stdio.
.venv/bin/python scripts/smoke_adk.py

# Run a single investigation end-to-end against the seeded data (live LLM calls).
PYTHONPATH=src .venv/bin/python scripts/test_investigate.py
```

There are also `seed_*.py` and `patch_*.py` scripts in `scripts/`
that populate the REAL demo SaaS accounts (Stripe test mode, HubSpot,
Intercom, Notion, …) with the customers, charges, disputes, and CRM
records the demo scenarios reference — including the $8,400 Aperture
pro-rata dispute fixture. Run them once when standing up a fresh demo
world; they are kept deliberately as the demo's data fixtures.

## What's NOT in this directory

- **The actor that fires actions.** That lives in
  `manthan-api/workers/actor.py`. The agent never holds write
  credentials.
- **The HTTP surface.** That lives in `manthan-api/` (the API and the
  three A2A agent services that mount this package's `a2a/` apps).
  This package speaks events and JSON-RPC payloads, not HTTP.
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
- Adding a specialist: add it in `team.build_specialists()` and wrap
  it in `ResilientAgentTool` so a hung sub-agent degrades instead of
  killing the run.
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
2. `loop.py` — the driver. Read top to bottom.
3. `team.py` + `adk_tools.py` — who investigates and what they can call.
4. `prompts.py` — what the coordinator is told to do.

Skip `pacer.py`, `state.py`, `llm.py`, `coral_session.py`,
`config.py` until you need them. They are mechanical.
