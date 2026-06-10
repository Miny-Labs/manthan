# Manthan · Track 3 refactor — end-to-end change plan

**Target:** Google for Startups AI Agents Challenge, Track 3 (GCP-native, ADK,
A2A, Gemini Enterprise / Marketplace-ready).
**Repo / branch:** `Miny-Labs/manthan` → `main`.
**Working copy:** `/Users/akshmnd/Dev Projects/manthan-track3`.
**Production (untouched):** `akash-mondal/manthan` → manthan.quest VPS.

---

## 1. Architecture thesis — seam-first, then native

**Where we are (present tense):** agents ARE the system. The investigator
agent service (`manthan-api/src/manthan_api/agents/investigator.py`) consumes
the `run_case()` Event stream **in-process** and writes its own events +
projections through `services/case_store.py`. There is no investigate worker —
it is DELETED; `workers/main.py` runs only the deterministic actor +
prettifier. See §4b.

**How we got here safely (history):** the port went seam-first. The legacy
NOTIFY-mirror worker (`manthan-api/src/manthan_api/workers/investigate.py`,
since deleted) consumed the agent through exactly one contract —
`async for evt in run_case(trigger, cfg, store)` mirroring each
`manthan_agent.types.Event` into Postgres. We kept `run_case()`'s signature
and `Event` stream identical and swapped only the internals from the custom
loop to ADK, so the Postgres schema, every API router, the actor (real Stripe
writes), the policy engine, and the existing UI needed **zero changes** while
the brain was replaced. Once the ADK spine was proven, the bolt-on was
removed: the worker's mirror logic was extracted verbatim into
`services/case_store.py` and the worker deleted. The seam held, then moved
inside the agent.

---

## 2. Model strategy (verified live on the provided AI Studio key)

All confirmed callable via `generativelanguage.googleapis.com` with
`GOOGLE_API_KEY` (header `x-goog-api-key`), `GOOGLE_GENAI_USE_VERTEXAI=FALSE`:

| Role | Model | Why |
|---|---|---|
| Orchestrator / Investigator | `gemini-3.1-pro-preview` | deep multi-source reasoning, 1M ctx |
| Specialists · advisor · operator chat | `gemini-3.5-flash` | fast, cheap, tool-capable |
| Triage router · event prettifier | `gemini-3.1-flash-lite` | cheapest tier |

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
| `config.py` | **DONE** — Gemini fields added | `google_api_key`, 3 model tiers, `gemini_use_vertexai`; sources kept; OpenRouter removed entirely |
| `pyproject.toml` | **DONE** | `openai` dropped; `google-adk>=2.2` + `google-genai>=2.8` added |
| `loop.py` | **DONE** — replaced | custom ReAct loop → ADK Agent + Runner, wrapped by a `run_case()` that yields the same `Event`s |
| `adk_tools.py` | **DONE** — new | ADK FunctionTools backed by Coral MCP; Evidence + integer citations live in ADK session state (preserves click-chip→source-row); version-tolerant catalog tool. (Plan said "transform `tools.py`"; what shipped was this new module — the legacy `tools.py` was deleted.) |
| `team.py` + `agents.py` | **DONE** — new | coordinator + FIVE parallel in-process specialists sharing one Evidence set (payments_analyst, customer_context, reliability_analyst, policy_analyst = Notion SOP retrieval/RAG, network_rules_analyst = own agent with built-in `google_search` grounding); `ResilientAgentTool` 180s cap |
| `pacer.py` + `adk_pacer.py` | **DONE** | pure rules kept; wired as `before_model_callback` (R1–R6 nudges/halt) + `before_tool_callback` on `conclude` (C1 refund-math gate) |
| `prompts.py` | **DONE** — light edits | SYSTEM prompt ports as the ADK agent instruction (the old REFLEXION self-check belonged to the deleted loop and is gone) |
| `types.py` | **KEEP** | `Event` already carries `trace_id`/`span_id`; all models reused |
| `state.py` | **KEEP** | `EventStore` stays as the translation buffer the investigator agent service passes in |
| `coral_session.py` | **KEEP** | `coral mcp-stdio` MCP client works as-is with 0.3.0 |

### 4b. `manthan-api/` (the de-bolting: agents ARE the system)

**Status: the native spine is in.** The NOTIFY-mirror investigate worker is
DELETED; the investigator agent service runs `run_case()` in-process and
writes its own events/projections through `services/case_store.py` (the
worker's mirror logic, extracted verbatim). Three macro agents, each its own
FastAPI app + Cloud Run service + service account:

| Area | Action | Notes |
|---|---|---|
| `agents/triage.py` | **NEW — done** | own card (`manthan-triage`); POST /webhooks/stripe (5-type fan-out moved here) + A2A `route_event`; calls the investigator via `manthan_agent.a2a.client.call_skill(INVESTIGATOR_A2A_URL, …)`, or in-process (local dev) when the URL is unset |
| `agents/investigator.py` | **NEW — done** | own card (`manthan-investigator`); A2A `investigate_dispute` creates the case (`insert_case_from_trigger`) then drives `run_case()` as an asyncio background task, mirroring every Event via `services.case_store` (`append_event` / `record_finding_projection` / `record_brief` / `finalize_case`); `investigation_started` dedupe guard kept |
| `agents/advisor.py` | **NEW — done** | own card (`manthan-advisor`); skills `ask` (one grounded Gemini call over PG findings+brief, cited by finding index, graceful no-key fallback), `precheck_refund` (deterministic rules), `get_customer_history`, `dispute_exposure`, `contribute_evidence` + the 6 reads |
| `services/case_store.py` | **NEW — done** | the worker's event append + projections as a service; the agent writes its own events |
| `services/a2a_store.py` | **EXTENDED — done** | PgCaseStore + the five advisor methods; shared pure helpers (`precheck_recommendation`, `run_ask`/`grounded_answer`) |
| `workers/investigate.py` | **DELETED** | replaced by `agents/investigator.py` — no NOTIFY hop, same rows |
| `workers/chat_loop.py` | **DELETED** | its only caller was the deleted worker; per-case Q&A is the advisor's `ask` |
| `workers/actor.py` + `workers/prettifier.py` | **KEEP** | deterministic executor + summaries; `workers/main.py` runs only these |
| `api/webhooks.py` | **TRANSFORMED — done** | back-compat surface: forwards to triage over A2A when `TRIAGE_A2A_URL` is set, else keeps the direct insert path |
| `api/a2a.py` | **TRANSFORMED — done** | gateway card (+advisor skills, points at the advisor when `ADVISOR_A2A_URL` set); dispatch routes advisor skills + sends `investigate_dispute` to the investigator |
| `api/chat.py` | **TRANSFORMED — done** | cross-case chat rides the same `grounded_answer` engine as the advisor's `ask` (AI Studio; OpenRouter dropped) |
| `adapters/stripe.py` | **KEEP** | real `Dispute.modify` / refund; idempotent |
| routers: cases, actions, events, inbox, audit, policy, citations, me, metrics, memory, sources, health | **KEEP** | merchant UI reads the same tables — zero UI changes |
| `schema/*.sql` | **KEEP** → Cloud SQL | unchanged; the agents write the same rows the worker did |

### 4c. `manthan-ui/` (keep app, add observability, drop demos)

| Area | Action | Notes |
|---|---|---|
| AppShell, Inbox, CaseList, Workspace, Approvals, Audit, Policy, Sources, SourceHealth, Memory, Metrics, Settings | **KEEP** | core product, reused by Track 3 |
| legal (Privacy/Terms/DPA), auth (Login/Signup/Onboarding) | **KEEP** | Onboarding repurposed to "paste Stripe restricted key" |
| **Agent Roster** | **NEW** | per-agent identity, model, A2A card URL, signing fingerprint |
| **Live Traces** | **NEW** | Cloud Trace span tree per case (tool calls, A2A hops, model calls) |
| **Agent Controls** | **NEW** | HITL tier thresholds, model pin, kill switch, token budget |
| `components/demo-v2/*`, `DemoTriggerMenu`, `ScenarioStory` | **DELETED — done** | demo wizards removed (with the `_legacy/` pages, `HeroShowcase`, and the orphaned story/demo assets) |
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
4. **Contract** — drive `run_case()` the way its real consumer does (the investigator agent service writing through `services/case_store.py`) against a temp Postgres; assert events/cases/findings/actions rows land identically to today.
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
| 8 | Cloud Trace + logging | ✅ done — only the GCP-connection check remains (spans in Trace explorer after first deployed run) |
| 9 | Stripe fan-out + triage | ✅ done — only the GCP-connection check remains (`stripe trigger` against a deployed webhook) |
| 10 | Observability UI (identity/traces/controls) | ✅ done |
| 11 | Native agent spine: triage/investigator/advisor macro agents; investigate worker deleted | ✅ done |
| 12 | Parallel specialist split (`team.py`/`agents.py`: 5 specialists, Notion SOP RAG + google_search grounding) | ✅ done |
| 13 | GCP deploy + secrets + evals + Marketplace | 🏗 scripted, awaiting GCP connection — `deploy/gcp/` (Dockerfiles, secrets/sql/deploy/pubsub scripts, runbook) + `docs/track3/SUBMISSION.md`; needs a real `PROJECT_ID` run + Producer Portal steps |

---

## 7. Notes / risks

- **Credential rotation:** the local Stripe test key, source tokens, and the
  AI Studio key live in `agent/.env` (gitignored) and have appeared in this
  build session's chat. Rotate them before any public submission/demo.
- **Coral upgrade for GCP:** local is 0.3.0; GCP target is 0.4.2 — carry the
  version-tolerant tool surface so both work.
- **Python 3.14 is too new** for clean ADK installs; the agent venv is pinned to 3.12.
