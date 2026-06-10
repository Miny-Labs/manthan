<p align="center">
  <img src="https://manthan.quest/banner.png" alt="Manthan - the operations layer for revenue disputes" />
</p>

<h3 align="center">Manthan</h3>

<p align="center">
  The operations layer for revenue disputes.
  <br />
  Settles chargebacks, refund requests, and failed payments in minutes - not days.
  <br /><br />
  <a href="#about-manthan"><strong>About</strong></a> ·
  <a href="#how-it-works"><strong>How it works</strong></a> ·
  <a href="#the-coral-data-plane"><strong>Coral</strong></a> ·
  <a href="#sources"><strong>Sources</strong></a> ·
  <a href="#features"><strong>Features</strong></a> ·
  <a href="#quick-start"><strong>Quick start</strong></a> ·
  <a href="#architecture"><strong>Architecture</strong></a> ·
  <a href="#self-hosting"><strong>Self-host</strong></a>
</p>

<p align="center">
  <a href="https://github.com/Miny-Labs/manthan/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License"></a>
  <a href="https://github.com/Miny-Labs/manthan/stargazers"><img src="https://img.shields.io/github/stars/Miny-Labs/manthan?style=flat&logo=github" alt="Stars"></a>
  <a href="https://github.com/Miny-Labs/manthan/pulse"><img src="https://img.shields.io/github/commit-activity/m/Miny-Labs/manthan?style=flat&logo=github" alt="Commits per month"></a>
  <a href="https://github.com/Miny-Labs/manthan/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs welcome"></a>
</p>

https://github.com/user-attachments/assets/afa2105c-3cc1-40e2-a799-6fcd2ee2f3f8

## About Manthan

Manthan is the autonomous investigator for B2B SaaS billing operations. The moment a chargeback hits Stripe, the **triage agent** picks up the webhook, dispatches the **investigator agent** over [A2A](https://a2a-protocol.org), and the investigator reads across **every connected system in one query**, drafts a **cited decision brief**, and queues the right actions for one-click human approval. External agents can also open a case directly by calling the `investigate_dispute` A2A skill.

A senior analyst spends 5 hours on a chargeback. Manthan spends 3 minutes.

The work happens on top of [**Coral**](https://github.com/withcoral/coral) - a unified Postgres-compatible SQL surface over 9 SaaS schemas. Instead of integrating 9 vendor SDKs and stitching results in Python, the agent writes one wide `SELECT` that joins Stripe + HubSpot + Intercom + Slack + Notion + PostHog + Sentry + Datadog + PagerDuty (plus Salesforce when CRM credentials are connected), gets one rowset back, and grounds every claim in the brief with a citation that links back to the underlying record. Click any citation chip and the source dashboard opens to the exact row.

## How it works

| Stage | What happens |
|---|---|
| **1 · Trigger** | A Stripe webhook (5 event types: `charge.dispute.created` · `charge.dispute.funds_withdrawn` · `charge.dispute.closed` · `radar.early_fraud_warning.created` · `invoice.payment_failed`) hits the triage agent on a per-org slug - or an external agent calls the `investigate_dispute` A2A skill. |
| **2 · Investigate** | The triage agent dispatches the investigator over A2A (with an in-process fallback in local dev). A coordinator fans out **five parallel specialists** that share one Evidence set, issue Coral SQL joins, and emit a live narrative you can watch in real time. |
| **3 · Brief** | A two-paragraph executive memo with the math shown, every number cited back to a source record - written straight to Postgres by the investigator itself. |
| **4 · Decide** | Recommends refund / fight / partial-credit / escalate, then **drafts the actions** (Stripe refund, dispute response, customer email, HubSpot note, Slack post), gated by the policy engine: auto-execute under $50, one-click approval $50-500, two-person approval $500+. |
| **5 · Approve** | One click. The deterministic actor fires each action against the real systems in a sequential cinematic - refund posts to Stripe, email lands in the inbox, HubSpot note appears, Slack pings. The **advisor agent** answers per-case operator questions afterwards. |

## The Coral data plane

Coral is what makes Manthan possible. It exposes 9 SaaS APIs as Postgres-compatible SQL schemas and ships as a single Rust binary the agent speaks to over [MCP](https://modelcontextprotocol.io) (Model Context Protocol) stdio.

**Why this matters.** The "default" autonomous-agent pattern is 1 round-trip per source - the LLM calls `get_stripe_dispute`, waits, calls `get_hubspot_company`, waits, calls `get_datadog_incidents`, waits, then tries to stitch JSON in its head. Manthan refuses to do that. The system prompt explicitly forbids one-shot lookups; the agent is required to **think in joins**:

```sql
-- The kind of query the agent actually writes (abridged):
SELECT
  -- payments + dispute context
  d.id AS dispute_id, d.amount, d.reason, d.evidence_due_by,
  ch.id AS charge_id, ch.created AS charge_created, ch.amount AS charge_amount,
  s.id AS subscription_id, s.status AS subscription_status, s.cancel_at_period_end,
  c.email AS customer_email, c.name AS customer_name,
  (SELECT COUNT(*) FROM stripe.disputes
     WHERE customer = d.customer AND id <> d.id) AS prior_disputes_total,

  -- CRM context
  sf.name AS sf_account, sf.industry, sf.annual_revenue, sf.billing_country,

  -- support history
  (SELECT COUNT(*) FROM intercom.conversations
     WHERE source_author_email = c.email) AS ic_conversations,
  (SELECT source_subject FROM intercom.conversations
     WHERE source_author_email = c.email
     ORDER BY created_at DESC LIMIT 1) AS ic_latest_subject,

  -- platform-health correlation (was there an outage during the disputed window?)
  (SELECT COUNT(*) FROM datadog.incidents
     WHERE service = 'custom-reports-svc'
       AND window @> tstzrange(ch.created, ch.created + interval '7 days')) AS incidents_in_window,

  -- documented policy
  (SELECT body FROM notion.pages
     WHERE title ILIKE '%pro-rata credit%' AND active = TRUE) AS policy_body
FROM stripe.disputes d
JOIN stripe.charges        ch ON ch.id = d.charge_id
LEFT JOIN stripe.subscriptions s ON s.customer = d.customer
JOIN stripe.customers      c  ON c.id = d.customer
LEFT JOIN salesforce.accounts sf ON sf.website ILIKE '%' || split_part(c.email,'@',2) || '%'
WHERE d.id = 'dp_aperture_345478';
```

One query. One round-trip. One rowset that contains everything needed to write the brief.

**How the integration works.**

1. The **investigator agent service** (`manthan-api/src/manthan_api/agents/investigator.py`) spawns the Coral binary as a subprocess via `coral mcp-stdio` for each investigation. (In the cloud deploy, Coral 0.4.2 can also run as a streamable-HTTP sidecar - see [`deploy/gcp/`](./deploy/gcp).)
2. The agent's `coral_sql`, `coral_list_catalog`, and `coral_describe_table` tools dispatch through that MCP session - shared by the coordinator and the four data specialists (the network-rules analyst carries only the built-in Google Search tool).
3. Coral fans the SQL out to each upstream API, normalizes results into Postgres-compatible rows, and returns one rowset.
4. Each query's `(seq, source, sql, rows, ms)` is recorded as an event; the Workspace's **Coral mode** renders the raw SQL feed alongside the prettified prose so operators can see exactly what the agent asked.

The agent brain lives in [`agent/`](./agent) - a [Google ADK](https://google.github.io/adk-docs/) multi-agent team on Gemini 3 (via [AI Studio](https://aistudio.google.com)), with the pacer's policy rules wired as ADK callbacks and every case artifact exposed over [A2A](https://a2a-protocol.org). The Coral binary is built from the sibling [`coral`](https://github.com/withcoral/coral) repo.

## Sources

**Read sources** - queried by the agent via Coral SQL:

| Schema | Tables used | What it grounds in the brief |
|---|---|---|
| `stripe`     | `disputes`, `charges`, `customers`, `subscriptions`, `refunds`, `invoices` | Payment + dispute facts, prior-dispute counts, subscription health |
| `hubspot`    | `companies`, `contacts`, `deals`, `notes` | ARR, tier, owner, prior notes |
| `intercom`   | `conversations`, `contacts` | Support history, recent subjects |
| `slack`      | `channels`, `messages` | Internal mentions, ops-channel context |
| `notion`     | `pages`, `blocks` | Policy docs, runbooks, post-mortems |
| `posthog`    | `events`, `persons` | Feature-usage signals during the disputed window |
| `sentry`     | `events`, `issues` | App errors correlated with the customer's session |
| `datadog`    | `incidents`, `metrics` | Service outages during the disputed window |
| `pagerduty`  | `incidents`, `users` | On-call history + ack/resolve timestamps |
| `salesforce` | `accounts`, `opportunities`, `contacts` | Customer-tier + revenue context (optional - registered when `SALESFORCE_*` credentials are present; not part of the default GCP bootstrap set) |

**Write / action adapters** - hit by the actor worker after operator approval, implemented natively (not via Coral) so we can return real `external_ref` IDs and idempotency keys:

| Adapter | Action |
|---|---|
| `stripe`  | `POST /v1/refunds`, `POST /v1/disputes/{id}/evidence` |
| `resend`  | `POST /emails` (templated, branded HTML; table-based for Outlook/Gmail compat) |
| `hubspot` | `POST /crm/v3/objects/companies/{id}/notes` |
| `slack`   | `chat.postMessage` to `#billing-ops` |
| `notion`  | Append resolution block to the case page |

When an upstream rejects an action (Stripe `charge_disputed`, Slack `channel_not_found`, etc.) the action is recorded as `failed` in the audit trail with the verbatim error; we never synthesize a success ref. The parallel actions still fire — e.g. the partial credit lands via `stripe_dispute_response` even when `stripe_refund` is rejected on a disputed charge.

## Features

- 🧠 **One-query investigations.** Coral exposes 9 SaaS schemas as Postgres-compatible SQL. The agent writes wide joins across them in a single round-trip, never per-source lookups.
- 🤖 **A real multi-agent team.** A coordinator (`gemini-3.1-pro-preview`) runs five specialists in parallel - payments, customer context, reliability, policy (Notion SOP retrieval), and network rules (Google Search grounding) - all sharing one Evidence set.
- 📑 **Cited brief.** Every claim in the postmortem carries a citation chip linking back to the underlying record (Stripe dispute, Notion page, Datadog incident). No fabrication - if it's in the brief, it's in a source.
- 🔴 **Live investigation cinematic.** Watch the agent query each source in real time, with brand glyphs inline in the narrative ("Manthan is asking 🟢 Intercom") and a running list of facts. Toggle the Coral panel to see the raw SQL feed.
- ✋ **Human-in-the-loop approval.** Policy-engine gates (auto under $50, one-click $50-500, two-person $500+). Operator reviews the brief, approves, and watches each action fire in a full-screen cinematic with per-action status and external-ref deep-links back to the source record (real Stripe refund / Resend email / HubSpot note / Slack ts).
- ✉️ **Branded customer emails.** Templated HTML emails (table-based for Outlook/Gmail compat) with summary cards, branded header, and policy-grounded reasoning - never the raw decision rationale.
- 🔒 **Per-user workspace isolation.** Every Clerk-authenticated user gets their own isolated org. Two operators can run investigations in parallel against the same demo data without seeing each other's cases.
- 📖 **Editorial UI.** The whole product reads like a magazine spread - Spectral serif, hairline rules, brand-colored source pills, Geist Mono for the data - not a SaaS dashboard.

## Quick start

### Prerequisites
- Node 20+ · `pnpm` or `npm`
- Python 3.12+ · [`uv`](https://github.com/astral-sh/uv)
- Docker (for the local Postgres)
- The [Coral binary](https://github.com/withcoral/coral) built and on your `PATH` (or pointed at via `CORAL_BINARY`)

### Setup
```sh
git clone https://github.com/Miny-Labs/manthan
cd manthan

# 1 · Environment - fill in GOOGLE_API_KEY (aistudio.google.com/apikey),
#                   CORAL_BINARY, STRIPE_API_KEY, HUBSPOT_ACCESS_TOKEN,
#                   SLACK_TOKEN, NOTION_API_KEY, CLERK_*
cp .env.example .env
cp manthan-api/.env.example manthan-api/.env
cp agent/.env.example agent/.env

# 2 · Database
docker compose -f manthan-api/docker-compose.yml up -d postgres

# 3 · Backend - the API gateway (opens cases and runs investigations
#     in-process in local dev) + the deterministic workers (actor + prettifier)
cd manthan-api && uv sync
uv run uvicorn manthan_api.main:app --reload --port 8000 &
uv run python -m manthan_api.workers.main &

#     Optional - run the three A2A agent services separately for full
#     cloud parity (each serves its own agent card):
# uv run uvicorn manthan_api.agents.triage:app --port 8001 &
# uv run uvicorn manthan_api.agents.investigator:app --port 8002 &
# uv run uvicorn manthan_api.agents.advisor:app --port 8003 &

# 4 · Frontend
cd ../manthan-ui && npm install && npm run dev
```

Visit **[http://localhost:5173](http://localhost:5173)** and sign in via Clerk. Fire a case by sending a Stripe `charge.dispute.created` test event to `/webhooks/stripe/{org}` (e.g. `stripe trigger charge.dispute.created`), or ask the agent over A2A: `POST /a2a` with skill `investigate_dispute`. The canonical seeded case — an $8,400 dispute that resolves to a $560 pro-rata credit — is dispute `du_1Tch1O…` in the test-mode Stripe account.

## Architecture

Three A2A macro agents are the system. The triage agent turns webhooks into triggers, the investigator agent runs the investigation and **writes its own events and projections** (no pipeline worker in between), and the advisor agent is the conversational face over the finished case.

```
   │ Stripe webhooks (5 event types)          │ A2A (external agents)
   │ dispute · fraud-warning · payment        │ message/send · tasks/get
   ▼                                          ▼
┌────────────────┐  A2A investigate_dispute ┌─────────────────────────────────┐
│  triage agent  │ ───────────────────────▶ │  investigator agent             │
│ flash-lite ·   │  (in-process fallback    │  gemini-3.1-pro-preview         │
│ per-org slug   │   in local dev)          │  coordinator + 5 parallel       │
└────────────────┘                          │  specialists, one Evidence set: │
                                            │   · payments_analyst            │
┌────────────────┐                          │   · customer_context            │
│ advisor agent  │                          │   · reliability_analyst         │
│ gemini-3.5-    │                          │   · policy_analyst (SOP RAG)    │
│ flash · ask /  │                          │   · network_rules_analyst       │
│ precheck_refund│                          │     (google_search grounding)   │
│ / history /    │                          │  pacer rules as ADK callbacks   │
│ exposure /     │                          └──────────┬──────────────────────┘
│ contribute +   │                                     │ Coral SQL over MCP
│ 6 reads        │                          ┌──────────▼──────────┐
└──────┬─────────┘                          │  Coral (mcp-stdio)  │
       │ per-case operator chat             │  9 SaaS schemas as  │
       │ lands as agent_reply               │  pg-SQL (read-only) │
       │                                    └─────────────────────┘
       │        both write through services/case_store.py
       ▼                                               ▼
┌────────────────────────────────────────────────────────────────┐
│   manthan-api · FastAPI + asyncpg · PostgreSQL                 │
│   • cases / events / findings / actions  (per-org PG schema)   │
│   • per-Clerk-user workspace isolation                         │
│   • SSE streams: /api/inbox/stream, /api/cases/:id/stream      │
│   • A2A: /.well-known/agent-card.json + JSON-RPC /a2a          │
└──────────────────────────┬─────────────────────────────────────┘
                           │ approved actions
                           │ (policy gates: auto <$50 ·
                           │  one-click $50-500 · two-person $500+)
                           ▼
              ┌──────────────────────┐      ┌──────────────────────┐
              │  workers/main.py     │ ───▶ │  Action adapters     │
              │  actor + prettifier  │      │  • Stripe · refunds  │
              │  (deterministic -    │      │  • Stripe · disputes │
              │   the only workers)  │      │  • Resend · emails   │
              └──────────────────────┘      │  • HubSpot · notes   │
                                            │  • Slack  · posts    │
                                            │  • Notion · blocks   │
                                            └──────────────────────┘
```

## Tech stack

**Frontend** · [React 19](https://react.dev) + [Vite](https://vite.dev) + [TypeScript](https://typescriptlang.org) · [Tailwind v4](https://tailwindcss.com) · [motion/react](https://motion.dev) · [Clerk](https://clerk.com) auth · deployable to Vercel or a static host.

**Backend** · [FastAPI](https://fastapi.tiangolo.com) + [asyncpg](https://github.com/MagicStack/asyncpg) · [PostgreSQL](https://www.postgresql.org) (cases, events, findings, actions) · three A2A agent services (`manthan_api.agents.triage` / `investigator` / `advisor`) + 2 deterministic background workers (`actor`, `prettifier` - both run by `manthan_api.workers.main`) coordinated via `FOR UPDATE SKIP LOCKED`.

**Agent** · `agent/` is a [Google ADK](https://google.github.io/adk-docs/) multi-agent team (`google-adk` 2.x) on Gemini via [AI Studio](https://aistudio.google.com): a coordinator plus five parallel specialists (`payments_analyst`, `customer_context`, `reliability_analyst`, `policy_analyst` = Notion SOP retrieval/RAG, `network_rules_analyst` = ADK's built-in `google_search` grounding) sharing one Evidence set (`team.py`, with a 180s `ResilientAgentTool` cap per specialist). Tools: `coral_sql`, `coral_list_catalog`, `coral_describe_table` (read, via Coral MCP) and `record_finding`, `ask_human`, `conclude` (in-loop, no external side effects). The pacer's money-mover invariants (R1-R6 + the C1 refund-math gate) run as ADK `before_model` / `before_tool` callbacks; the run is OpenTelemetry-instrumented end to end (Cloud Trace exporter via the `gcp` extra). See [`agent/README.md`](./agent/README.md) for the tool surface.

**A2A** · every case artifact — cases, briefs, findings, actions, the audit trail — is pickup-able by external agents over the [A2A protocol](https://a2a-protocol.org): Agent Card at `/.well-known/agent-card.json`, JSON-RPC at `/a2a`. Skills: `investigate_dispute` (action), the advisor's `ask` / `precheck_refund` / `get_customer_history` / `dispute_exposure` / `contribute_evidence`, plus 6 state reads (`get_case`, `list_cases`, `get_brief`, `get_findings`, `get_actions`, `get_audit_trail`).

**Data plane** · [Coral](https://github.com/withcoral/coral) - Rust binary, 9 SaaS schemas as Postgres SQL. Local: spawned per-investigation via `coral mcp-stdio`. Cloud: Coral 0.4.2 streamable-HTTP sidecar.

**Models** (Gemini, via AI Studio - `GOOGLE_API_KEY`, `GOOGLE_GENAI_USE_VERTEXAI=FALSE`)
- Triage agent + the live "prettifier": `gemini-3.1-flash-lite`
- Investigator coordinator: `gemini-3.1-pro-preview`
- Specialist sub-agents + the advisor: `gemini-3.5-flash`

**External services** · [Stripe](https://stripe.com) (payments + disputes) · [Resend](https://resend.com) (outbound transactional email - the actor's approved customer emails) · [HubSpot](https://hubspot.com) (CRM notes) · [Slack](https://slack.com) (post-approval ops notifications) · [Notion](https://notion.so) (policy + resolution blocks).

## Self-hosting

Manthan ships as the API gateway + three A2A agent services + a deterministic worker + a Postgres + a Vite static frontend + the Coral subprocess. **Google Cloud is the supported path** — Cloud Run for every service (each agent under its own per-agent service account), Cloud SQL Postgres, Secret Manager (per-tenant `coral-{tenant}-{credential}` secrets with per-secret IAM), and Cloud Trace. Vertex AI Agent Engine is documented as an alternative host for the investigator. The full runbook, Dockerfiles, and bootstrap scripts live in [`deploy/gcp/`](./deploy/gcp).

## Contributing

Issues and PRs welcome. Before pushing:

- `cd manthan-ui && npm run typecheck`
- `cd manthan-api && uv run pytest`
- `cd agent && uv run pytest`

Keep PRs scoped to a single concern; we squash-merge.

## License

Apache 2.0 - see [`LICENSE`](./LICENSE). A hosted version runs at [manthan.quest](https://manthan.quest), maintained in a separate production repository with managed Coral connections, OAuth onboarding, and Clerk-issued workspaces.

---

<sub>Built by <a href="https://miny-labs.com">miny-labs</a> · Made with 🪸 Coral</sub>
