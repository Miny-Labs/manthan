# Manthan — Track 3 submission map

**Track:** Google for Startups AI Agents Challenge, Track 3
(GCP-native, ADK, A2A, Gemini Enterprise / Marketplace-ready).
**Full change plan + status:** [PLAN.md](./PLAN.md)
**Deploy runbook:** [`deploy/gcp/README.md`](../../deploy/gcp/README.md)

This file maps each judging criterion to what exists in the repo. Items
marked `TODO` are the remaining human steps (mostly portal clicks and
live runs) — the code/scripts behind them are already in place.

## 0. The system in one paragraph (current architecture)

Three A2A macro agents are the system
(`manthan-api/src/manthan_api/agents/`): **triage**
(`gemini-3.1-flash-lite`) takes the Stripe webhook (5 event types),
resolves the org, and dispatches the **investigator**
(`gemini-3.1-pro-preview`) over A2A (with an in-process fallback for
local dev). The investigator creates the case and runs the ADK
investigation in-process — a coordinator fanning out FIVE parallel
specialists over one shared Evidence set (payments, customer context,
reliability, policy = Notion SOP retrieval/RAG, network rules = its own
agent with built-in `google_search` grounding) — and writes its own
events and projections through `services/case_store.py`. The **advisor**
(`gemini-3.5-flash`) is the conversational A2A face (`ask`,
`precheck_refund`, `get_customer_history`, `dispute_exposure`,
`contribute_evidence` + 6 reads) and answers per-case operator chat.
Drafted actions pass the HITL policy gates (auto under $50 / one-click
$50–500 / two-person $500+) before the deterministic actor executes;
`workers/main.py` runs only the actor + prettifier. Coral is the
grounding plane over nine live SaaS schemas (stdio locally, a 0.4.2
streamable-HTTP sidecar on GCP), and every claim carries a clickable
citation back to the source row.

**Live-run proof points** (local end-to-end run against the seeded
$8,400 `product_not_received` dispute `du_1Tch1OCNe0SBMhzIAppAdJjT`):

- All **five specialists ran in parallel** under the coordinator and
  contributed to a single shared Evidence set — the brief's citations
  span payments, CRM/support, reliability, the Notion SOP, and
  Google-Search-grounded network rules.
- The **pacer's C1 refund-math gate rejected a math-less `conclude` on
  camera**: the coordinator first tried to conclude without showing the
  refund arithmetic, the `before_tool_callback` bounced it, and the
  next attempt carried the worked-out math.
- The final brief recommended a **$560 partial refund at 0.95
  confidence** on the $8,400 dispute — drafted, policy-gated (two-person
  tier), and never auto-executed.

---

## 1. ADK agent (the brain)

The investigator is a Google ADK agent that drives Gemini over the Coral
SQL data plane (MCP) to investigate billing disputes end-to-end.

| Piece | File |
|---|---|
| ADK Agent + Runner wrapped in the legacy-compatible `run_case()` event stream | `agent/src/manthan_agent/loop.py` |
| Coordinator + FIVE parallel in-process specialists (payments_analyst, customer_context, reliability_analyst, policy_analyst = Notion SOP RAG, network_rules_analyst = `google_search` grounding), `ResilientAgentTool` 180s cap | `agent/src/manthan_agent/team.py` |
| ADK agent definitions / model wiring for the team | `agent/src/manthan_agent/agents.py` |
| ADK FunctionTools over Coral MCP (evidence + integer citations in session state) | `agent/src/manthan_agent/adk_tools.py` |
| Pacing/guard rules as ADK callbacks (`before_model` nudges R1–R6, `before_tool` refund-math gate C1) | `agent/src/manthan_agent/adk_pacer.py` (pure rules: `pacer.py`) |
| Agent instruction (SYSTEM prompt) | `agent/src/manthan_agent/prompts.py` |
| Typed event/finding/brief models (incl. `trace_id`/`span_id` on `Event`) | `agent/src/manthan_agent/types.py` |
| Coral `mcp-stdio` session management | `agent/src/manthan_agent/coral_session.py` |
| Stripe-event triage (pure function `trigger_from_stripe_event`) | `agent/src/manthan_agent/triage.py` (tests: `agent/tests/test_triage.py`, fan-out: `manthan-api/tests/test_stripe_fanout.py`) |

**The seam:** the investigator agent service
(`manthan-api/src/manthan_api/agents/investigator.py`) consumes the agent
solely via `async for evt in run_case(trigger, cfg, store)` — run
in-process, with every Event written to Postgres through
`services/case_store.py`. The ADK port changed the internals, not the
contract; once proven, the legacy NOTIFY-mirror worker was deleted and
its mirror logic moved verbatim into `case_store.py`.

## 2. A2A — card + skills

| Piece | File |
|---|---|
| AgentCard builder (`build_agent_card(base_url, identity)` — provider, model, signing-key fingerprint, runtime) | `agent/src/manthan_agent/a2a/card.py` |
| JSON-RPC server (`message/send`, `tasks/get`) + Starlette app | `agent/src/manthan_agent/a2a/server.py` |
| `CaseStore` protocol + in-memory impl | `agent/src/manthan_agent/a2a/store.py` |
| FastAPI mounting: card at `/.well-known/agent-card.json`, RPC at `POST /a2a` | `manthan-api/src/manthan_api/api/a2a.py` (store: `services/a2a_store.py`, tests: `manthan-api/tests/test_a2a_router.py`) |
| Tests (pure-logic) | `agent/tests/test_a2a.py` |

**Skills:** the action skill `investigate_dispute`, the advisor skills
`ask` (grounded, cited NL answer over a case), `precheck_refund`,
`get_customer_history`, `dispute_exposure`, and `contribute_evidence`
(external agents push evidence into an open case), plus six state
queries (`get_case`, `list_cases`, `get_brief`, `get_findings`,
`get_actions`, `get_audit_trail`) — so a partner agent can *delegate* an
investigation, *consult* it mid-flight, and *pick up* every artifact it
produced. Each macro agent (triage / investigator / advisor) publishes
its own card; the gateway card at `/.well-known/agent-card.json`
aggregates them. `A2A_PUBLIC_URL` is stamped into the card by
`deploy/gcp/deploy.sh`.

## 3. Gemini 3 via AI Studio

| Role | Model |
|---|---|
| Orchestrator / investigator | `gemini-3.1-pro-preview` |
| Specialists · advisor · operator chat | `gemini-3.5-flash` |
| Triage router · event prettifier | `gemini-3.1-flash-lite` |
| Action execution (the actor) | deterministic — no model |

Auth: plain `GOOGLE_API_KEY` (AI Studio,
`GOOGLE_GENAI_USE_VERTEXAI=FALSE`) — no Vertex dependency. Config fields
`google_api_key` / `model` / `model_subagent` / `model_triage` in
`agent/src/manthan_agent/config.py`; client helper in
`agent/src/manthan_agent/llm.py`. The deploy wires the key from Secret
Manager (`manthan-{tenant}-gemini-api-key`).

## 4. Cloud Trace / observability

- Trace wiring: `agent/src/manthan_agent/tracing.py`
  (tests: `agent/tests/test_tracing.py`); `Event` carries
  `trace_id`/`span_id` (`agent/src/manthan_agent/types.py`), emitted
  through `run_case()` and written to Postgres by the investigator agent
  itself (`services/case_store.py`), so every UI event row is joinable
  to a trace.
- Observability UI (routed in `manthan-ui/src/AppRouter.tsx`):
  `manthan-ui/src/pages/AgentRoster.tsx` (per-agent identity/model/card,
  card fetch via `src/lib/agentCard.ts`), `AgentTraces.tsx` (span tree
  per case), `AgentControls.tsx` (HITL thresholds, model pin, kill
  switch).
- Cloud Run runtime SA gets `roles/cloudtrace.agent`
  (`deploy/gcp/README.md` §2); `cloudtrace.googleapis.com` enabled in §1.
- ADK auto-instruments model/tool spans; Coral's `[otel]` feature nests
  SQL spans beneath tool calls.
- `TODO (verify live)`: confirm exported spans appear in the Cloud Trace
  explorer after the first deployed investigation (PLAN task 8 is
  done-pending-verify).

## 5. HITL policy engine

- Schema: `manthan-api/schema/003_policy_engine.sql` (policy rules,
  thresholds, approval tiers).
- API: `manthan-api/src/manthan_api/api/policy.py` (rules CRUD),
  `actions.py` + `inbox.py` (approve/reject flow), `audit.py`
  (immutable audit trail).
- Executor: `manthan-api/src/manthan_api/workers/actor.py` performs the
  real writes (Stripe refund/dispute update, email, Slack) **only after
  human approval**; `adapters/stripe.py` is idempotent.
- UI: `manthan-ui/src/pages/Approvals.tsx`, `Policy.tsx`, `Audit.tsx`.
- Agent side: the `conclude` tool drafts actions, never executes them —
  the refund-math gate (`adk_pacer.py` C1) blocks over-refund drafts
  before they even reach a human.

## 6. Eval strategy

Layered, per PLAN.md §5:

1. Pure-logic unit suites (no network, no LLM — 150 passing: 79 in
   `agent/tests`, 71 in `manthan-api/tests`):
   `agent/tests/test_pacer.py`, `test_pacer_callbacks.py`,
   `test_adk_tools.py`, `test_a2a.py`, `test_tracing.py`,
   `test_triage.py`, the team/specialist suites —
   `cd agent && PYTHONPATH=src .venv/bin/python -m pytest tests -q` —
   plus `manthan-api/tests/` (a2a router, stripe fan-out, agent apps).
2. Integration against live local Coral + the seeded dispute
   `du_1Tch1OCNe0SBMhzIAppAdJjT` ($8,400, product_not_received):
   ≥5 cited findings, decision + drafted actions, resolvable citations.
   Latest run: 5 specialists in parallel, C1 rejected a math-less
   `conclude`, final brief = $560 refund at 0.95 confidence (see §0).
3. Contract test: drive `run_case()` the way its real consumer does —
   the investigator agent service writing through
   `services/case_store.py` — against a temp Postgres; assert identical
   events/cases/findings/actions rows.
4. ADK eval set: synthetic disputes scored on
   `tool_trajectory_avg_score` + brief quality.
   `TODO`: commit the eval set under `agent/evals/` and record baseline
   scores here.

## 7. Marketplace readiness (Producer Portal checklist)

Legal/trust surfaces already exist in the product:
`manthan-ui/src/pages/Privacy.tsx`, `Terms.tsx`, `DPA.tsx`.

- [ ] Rotate every credential that appeared during development
      (Stripe test key, source tokens, AI Studio key) — PLAN.md §7.
- [ ] Deploy to a clean GCP project via `deploy/gcp/README.md` (steps 1–7).
- [ ] Verify the public agent card:
      `curl $API_URL/.well-known/agent-card.json`.
- [ ] Google Cloud Marketplace Producer Portal: create the producer
      account + accept the publisher agreement.
- [ ] Register the product (SaaS / AI agent listing), attach the project.
- [ ] Listing content: name, logo, descriptions, category, pricing model,
      support contacts.
- [ ] Link public Trust Center pages (Privacy / Terms / DPA URLs from the
      deployed UI domain).
- [ ] Security questionnaire + data-handling disclosure (data accessed:
      Stripe/HubSpot/Intercom/Slack/Notion/PagerDuty/Datadog/Sentry/PostHog,
      keys-only auth, per-tenant Secret Manager isolation).
- [ ] Integration review: A2A card reachable, health endpoint
      (`/healthz`), webhook setup docs.
- [ ] Submit for review; track feedback to closure.

## 8. Deployability (this repo → a clean project)

Everything scripted under `deploy/gcp/` (validated with `bash -n`;
Coral v0.4.2 release asset + sha256 verified against GitHub):
`Dockerfile.api`, `Dockerfile.ui` + `Caddyfile`, `secrets-bootstrap.sh`,
`sql-migrate.sh`, `deploy.sh`, `coral-bootstrap.sh`,
`worker-entrypoint.sh`, `pubsub-setup.sh`, and the numbered runbook in
`deploy/gcp/README.md` — the only human input is a real `PROJECT_ID`
plus portal steps listed in the runbook's "What is NOT automated".
