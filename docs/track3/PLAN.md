# Manthan · Track 3 refactor — end-to-end change plan

**Target:** Google for Startups AI Agents Challenge, Track 3 (GCP-native, ADK,
A2A, Gemini Enterprise / Marketplace-ready).
**Repo / branch:** `Miny-Labs/manthan` → `main`.
**Working copy:** `/Users/akshmnd/Dev Projects/manthan-track3`.
**Production (untouched):** `akash-mondal/manthan` → manthan.quest VPS.

---

## 1. Architecture thesis — the seam that makes this safe

The existing worker consumes the agent through exactly one contract:

```python
# manthan-api/src/manthan_api/workers/investigate.py
async for evt in run_case(trigger, cfg, store):   # yields manthan_agent.types.Event
    await self._mirror_event(... evt ...)          # -> Postgres events/cases/findings/actions
```

So we **keep `run_case()`'s signature and `Event` stream identical** and swap
only the internals from the custom loop to ADK. The worker, Postgres schema,
every API router, the actor (real Stripe writes), policy engine, and the
existing UI then need **zero changes** to keep working. New Track-3 surfaces
(A2A, traces, observability UI) are added *on top*, not woven through.

This is the single most important decision: blast radius = the `agent/` package
+ additive infra. Everything downstream is preserved.

---

## 2. Model strategy (verified live on the provided AI Studio key)

All confirmed callable via `generativelanguage.googleapis.com` with
`GOOGLE_API_KEY` (header `x-goog-api-key`), `GOOGLE_GENAI_USE_VERTEXAI=FALSE`:

| Role | Model | Why |
|---|---|---|
| Orchestrator / Investigator | `gemini-3.1-pro-preview` | deep multi-source reasoning, 1M ctx |
| Sub-agents (actions, chat, prettifier) | `gemini-3-flash-preview` | fast, cheap, tool-capable |
| Triage / event router | `gemini-3.1-flash-lite` | cheapest, classify-and-route |

ADK owns the Investigator's model calls via `Agent(model=...)`. The
`agent/src/manthan_agent/llm.py` helper (google-genai, AI Studio) serves the
remaining non-agent generations (prettifier, cross-case chat).

---

## 3. Coral findings (local 0.3.0+13b6d2f)

- **MCP tool surface is version-dependent.** 0.3.0 exposes `sql` + `list_tables`;
  0.4.x exposes `sql` + `list_catalog` + `search_catalog` + `describe_table` +
  `list_columns`. **Ported tools must detect and adapt** (done in the smoke test;
  to be carried into `tools.py`).
- **Live local schemas (9):** stripe, intercom, notion, slack, datadog, sentry,
  posthog, pagerduty, salesforce. `slack.messages` + `thread_replies` ARE
  queryable here (richer than the prompt cheatsheet assumes).
- **`hubspot` + `zendesk` don't load** on this binary ("unknown bundled source").
  salesforce substitutes for CRM locally. For GCP we pin Coral **0.4.2** and
  reinstall the full source set via Secret-Manager-templated configs.
- Real seeded data present: dispute `du_1Tch1OCNe0SBMhzIAppAdJjT`, $8,400,
  `product_not_received` — our end-to-end test fixture.

---

## 4. Component-by-component change plan

### 4a. `agent/` (the brain — where the real work is)

| File | Action | Notes |
|---|---|---|
| `llm.py` | **DONE** — rewritten | OpenRouter → google-genai AI Studio helper |
| `config.py` | **DONE** — Gemini fields added | `google_api_key`, 3 model tiers, `gemini_use_vertexai`; sources kept; OpenRouter optional |
| `pyproject.toml` | **DONE** | `openai` dropped; `google-adk>=2.2` + `google-genai>=2.8` added |
| `loop.py` | **REPLACE** | custom ReAct loop → ADK Agent + Runner, wrapped by a new `run_case()` that yields the same `Event`s |
| `tools.py` | **TRANSFORM** | 6 tools → ADK FunctionTools backed by Coral MCP; Evidence + integer citations live in ADK session state (preserves click-chip→source-row); version-tolerant catalog tool |
| `pacer.py` | **TRANSFORM** | pure rules KEPT; wire as `before_model_callback` (R1–R6 nudges/halt) + `before_tool_callback` on `conclude` (C1 refund-math gate) |
| `prompts.py` | **KEEP** (light edits) | SYSTEM/REFLEXION prompts port as the ADK agent instruction |
| `types.py` | **KEEP** | `Event` already carries `trace_id`/`span_id`; all models reused |
| `state.py` | **KEEP** | `EventStore` stays as the translation buffer the worker passes in |
| `coral_session.py` | **KEEP** | `coral mcp-stdio` MCP client works as-is with 0.3.0 |
| `agent.py` | **NEW** | ADK Agent definitions (investigator + later triage/actions) + callbacks |

### 4b. `manthan-api/` (mostly preserved)

| Area | Action | Notes |
|---|---|---|
| `workers/investigate.py` | **KEEP (untouched)** | consumes `run_case()` — the seam |
| `workers/actor.py` | **KEEP** | real Stripe/email/Slack/HubSpot/Notion writes after approval |
| `adapters/stripe.py` | **KEEP** | real `Dispute.modify` / refund; idempotent |
| routers: cases, actions, events, inbox, audit, policy, citations, me, metrics, memory, sources, health, webhooks | **KEEP** | core case/brief/action/audit infra + Stripe webhook |
| `schema/*.sql` (5 migrations) | **KEEP** → port to Cloud SQL | events/cases/findings/actions/policy_rules etc. |
| `webhooks.py` | **TRANSFORM** | Stripe fan-out: 5 event types → triage → investigator |
| `workers/prettifier.py` | **TRANSFORM** | OpenRouter → AI Studio (`llm.agenerate_text`) |
| `config.py` | **TRANSFORM** | add `GOOGLE_API_KEY`; drop `OPENROUTER_API_KEY` reliance |
| `api/demo.py`, `demo_v2.py`, `demo_v3.py` | **DELETE** | Maya-email + Vermillion-Slack demo wizards |
| `api/email_webhook.py`, `services/email_*`, `services/resend_inbound.py`, `adapters/resend.py` | **DELETE/RETIRE** | inbound-email (Maya) demo path |
| `api/slack.py`, `services/slack_bot.py`, `services/slack_notifier.py`, `adapters/slack.py` | **DELETE/RETIRE** | Vermillion Slack demo (actor's slack post can stay as an action kind) |
| `api/chat.py`, `api/narrative.py`, `workers/chat_loop.py` | **DEFER** | low priority; keep until A2A query skills supersede |

### 4c. `manthan-ui/` (keep app, add observability, drop demos)

| Area | Action | Notes |
|---|---|---|
| AppShell, Inbox, CaseList, Workspace, Approvals, Audit, Policy, Sources, SourceHealth, Memory, Metrics, Settings | **KEEP** | core product, reused by Track 3 |
| legal (Privacy/Terms/DPA), auth (Login/Signup/Onboarding) | **KEEP** | Onboarding repurposed to "paste Stripe restricted key" |
| **Agent Roster** | **NEW** | per-agent identity, model, A2A card URL, signing fingerprint |
| **Live Traces** | **NEW** | Cloud Trace span tree per case (tool calls, A2A hops, model calls) |
| **Agent Controls** | **NEW** | HITL tier thresholds, model pin, kill switch, token budget |
| `components/demo-v2/*`, `DemoTriggerMenu`, `ScenarioStory` | **DELETE** | demo wizards |
| blog/marketing-heavy pages | **TRIM** | optional; not core to Track 3 judging |

### 4d. Infra (additive, GCP-native)

| Piece | Action |
|---|---|
| Cloud Run service(s) + Dockerfile | **NEW** — investigator (+ later triage/actions) as A2A services |
| Cloud SQL Postgres | **NEW** — port 5 migrations, private IP, IAM auth |
| Secret Manager | **NEW** — `coral-{tenant}-{var}` naming, per-secret IAM, boot script |
| Pub/Sub | **NEW (cloud)** — replaces PG NOTIFY for inter-service; NOTIFY kept for local |
| Cloud Trace + Logging | **NEW** — OTel exporter; ADK auto-instruments; Coral `[otel]` nests SQL spans |
| A2A | **NEW** — `to_a2a()` Agent Card + state query skills |
| ADK eval suite | **NEW** — `tool_trajectory_avg_score` + brief-quality on synthetic disputes |
| Trust Center / Marketplace | **NEW** — public ToS/DPA/Privacy + Producer Portal scaffolding |

---

## 5. Testing strategy (engineered, not just smoke)

1. **Foundation smoke** — `agent/scripts/smoke_adk.py` (genai + ADK tool + Coral). ✅ green.
2. **Unit** — pacer rules (pure fns), citation index resolution, tool arg validation, version-tolerant catalog mapping.
3. **Integration (real data)** — run the ported ADK agent against live local Coral on `du_1Tch1O…`; assert ≥5 cited findings, a Brief with decision + drafted actions, resolvable citations.
4. **Contract** — drive `run_case()` through the real worker against a temp Postgres; assert events/cases/findings/actions rows land identically to today.
5. **ADK eval** — synthetic dispute set scored on tool trajectory + brief quality (reliability metric for the submission).

---

## 6. Status

| # | Task | State |
|---|---|---|
| 1 | Foundation: ADK + AI Studio Gemini 3 + Coral smoke | ✅ done |
| 2 | Replace OpenRouter with AI Studio client | ✅ done |
| 3 | Port Investigator loop → ADK Agent | ✅ done |
| 4 | Port pacer → ADK callbacks | ✅ done |
| 5 | Preserve `run_case()` Event contract | ✅ done |
| 6 | E2E test vs real seeded dispute | ✅ done |
| 7 | A2A: expose all state as pickup-able skills | ✅ done |
| 8 | Cloud Trace + logging | ✅ done — pending live verify (spans in Trace explorer after first deployed run) |
| 9 | Stripe fan-out + triage | ✅ done — pending live verify (`stripe trigger` against a deployed webhook) |
| 10 | Observability UI (identity/traces/controls) | ✅ done |
| 11 | GCP deploy + secrets + evals + Marketplace | 🏗 scaffolded — `deploy/gcp/` (Dockerfiles, secrets/sql/deploy/pubsub scripts, runbook) + `docs/track3/SUBMISSION.md`; needs a real `PROJECT_ID` run + Producer Portal steps |

---

## 7. Notes / risks

- **Credential rotation:** the local Stripe test key, source tokens, and the
  AI Studio key live in `agent/.env` (gitignored) and have appeared in this
  build session's chat. Rotate them before any public submission/demo.
- **Coral upgrade for GCP:** local is 0.3.0; GCP target is 0.4.2 — carry the
  version-tolerant tool surface so both work.
- **Python 3.14 is too new** for clean ADK installs; the agent venv is pinned to 3.12.
