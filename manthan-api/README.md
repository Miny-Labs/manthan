# manthan-api

Multi-tenant backend for Manthan: the HTTP gateway, the three A2A macro
agent services (triage / investigator / advisor), two deterministic
background workers (actor + prettifier), and the action-adapter layer
around the agent brain. Speaks to `manthan-ui` over JSON + SSE and
imports `manthan-agent` as a library.

## Quick start (local dev)

```bash
# 1. Start Postgres
docker compose up -d postgres

# 2. Install Python deps with uv
uv sync

# 3. Copy + edit env
cp .env.example .env
# fill in GOOGLE_API_KEY (AI Studio), CLERK_*, STRIPE_*, RESEND_*, CORAL_BINARY

# 4. Apply the schema (only the first time)
docker exec -i manthan-postgres psql -U manthan -d manthan \
    < schema/001_initial.sql
docker exec -i manthan-postgres psql -U manthan -d manthan \
    < schema/002_event_summary.sql
docker exec -i manthan-postgres psql -U manthan -d manthan \
    < schema/003_policy_engine.sql
docker exec -i manthan-postgres psql -U manthan -d manthan \
    < schema/004_citation_reasonings.sql
docker exec -i manthan-postgres psql -U manthan -d manthan \
    < schema/005_auth_signups.sql

# 5. Seed a dev org + admin member
uv run python -m manthan_api.scripts.bootstrap_dev_org

# 6. Run the gateway + the deterministic workers (each in its own
#    terminal). This is enough for the UI: with INVESTIGATOR_A2A_URL
#    unset, the Stripe webhook handler falls back to the in-process
#    investigator, so cases still run end-to-end.
uv run uvicorn manthan_api.main:app --reload --port 8000
uv run python -m manthan_api.workers.main           # actor + prettifier

# 6b. Optional - full multi-service parity with the cloud topology:
#     run each agent as its own app and wire the URLs
#     (INVESTIGATOR_A2A_URL, TRIAGE_A2A_URL, ADVISOR_A2A_URL).
uv run uvicorn manthan_api.agents.triage:app --port 8081
uv run uvicorn manthan_api.agents.investigator:app --port 8082
uv run uvicorn manthan_api.agents.advisor:app --port 8083
```

Verify:

```bash
curl http://localhost:8000/healthz
# {"status":"ok","version":"0.1.0"}

curl http://localhost:8000/readyz
# {"status":"ready","db":true,"version":"0.1.0"}

curl -H "X-Manthan-Dev-Org: acme" http://localhost:8000/api/cases
# {"cases":[],"total":0}

curl http://localhost:8000/.well-known/agent-card.json
# the A2A agent card (gateway aggregate)
```

## Layout

```
manthan-api/
├── pyproject.toml
├── docker-compose.yml           # local Postgres (dev dependency)
├── schema/                      # forward-only PG migrations
│   ├── 001_initial.sql          # orgs, members, cases, events, findings, actions
│   ├── 002_event_summary.sql    # prettifier output cache
│   ├── 003_policy_engine.sql    # policy_rules + match log
│   ├── 004_citation_reasonings.sql
│   └── 005_auth_signups.sql     # waitlist for the hosted version
└── src/manthan_api/
    ├── main.py                  # FastAPI gateway entry, router wiring
    ├── config.py                # env-driven settings
    ├── db.py                    # asyncpg pool + JSONB codec
    ├── models.py                # Pydantic request/response shapes
    ├── api/                     # HTTP routers
    │   ├── health.py            #   /healthz · /readyz
    │   ├── me.py                #   /api/me  (Clerk-resolved tenant)
    │   ├── inbox.py             #   /api/inbox/stream  (SSE)
    │   ├── cases.py             #   /api/cases · /api/cases/{id}
    │   ├── actions.py           #   /api/cases/{id}/approve · /hold · /deny
    │   ├── events.py            #   /api/cases/{id}/events  (SSE)
    │   ├── chat.py              #   /api/chat  (cross-case chat, grounded answers)
    │   ├── policy.py            #   policy CRUD + match history
    │   ├── audit.py             #   /api/audit
    │   ├── citations.py         #   citation deep-links + reasoning chips
    │   ├── narrative.py         #   live investigation narrative
    │   ├── memory.py            #   per-org knowledge memory
    │   ├── metrics.py           #   counters for the inbox header
    │   ├── sources.py           #   connected-source roster + Coral detail
    │   ├── a2a.py               #   root agent card + JSON-RPC /a2a gateway
    │   ├── webhooks.py          #   /webhooks/stripe/{org}  (back-compat intake)
    │   └── clerk_webhook.py     #   /webhooks/clerk         (welcome email)
    ├── middleware/
    │   └── tenant.py            # org + member resolver (Clerk + dev bypass)
    ├── agents/                  # the three A2A agent services (own apps)
    │   ├── triage.py            #   Stripe intake + route_event -> investigator
    │   ├── investigator.py      #   runs the ADK agent in-process, writes events
    │   └── advisor.py           #   ask/precheck_refund/… conversational face
    ├── workers/                 # deterministic workers only (no agent here)
    │   ├── main.py              #   starts actor + prettifier
    │   ├── actor.py             #   drains approved actions to the adapters
    │   └── prettifier.py        #   generates event summaries for the UI
    ├── adapters/                # external-write integrations (not via Coral)
    │   ├── stripe.py            #   refunds + dispute evidence
    │   ├── hubspot.py           #   CRM notes
    │   ├── slack.py             #   chat.postMessage + thread replies
    │   ├── resend.py            #   templated transactional email
    │   └── notion.py            #   appended resolution blocks
    ├── services/                # cross-router helpers (case_store, a2a_store,
    │                            #   policy engine, citation links/reasoning,
    │                            #   email dispatcher + templates)
    └── scripts/                 # bootstrap_dev_org
```

## Architecture

The events table is the single source of truth (12-Factor Agents #3
and #5: events drive everything, state is derived). The frontend reads
from projection tables (`cases`, `actions`, `findings`, plus the
`summary` column on `events`); the audit log reads from `events`
directly.

Three A2A macro agents, two deterministic workers, one gateway:

| Service | Job |
|---|---|
| `agents/triage.py` | Stripe webhook intake (5 event types, per-org slug) + the `route_event` A2A skill. Dispatches the investigator over A2A (`INVESTIGATOR_A2A_URL`), with an in-process fallback for local dev. |
| `agents/investigator.py` | Accepts `investigate_dispute`, opens the case, runs the ADK agent loop (`manthan_agent.loop.run_case`) **in-process**, and writes its own events/projections via `services/case_store.py` — no NOTIFY hop. |
| `agents/advisor.py` | Conversational A2A face: `ask` / `precheck_refund` / `get_customer_history` / `dispute_exposure` / `contribute_evidence` + the 6 read skills. Powers the per-case operator chat (`agent_reply`). |
| `workers/main.py` → actor | Drains approved actions from the actions queue, dispatches to the adapter (`stripe.py`, `resend.py`, etc.), records the external_ref + status. Idempotent via `actions.idempotency_key`. |
| `workers/main.py` → prettifier | Walks unsummarized events (tool_call / tool_result / finding_recorded …) and writes a one-line human-readable summary. Drives the "Manthan is asking Stripe…" live narrative in the workspace. |

See [`../agent/README.md`](../agent/README.md) for the agent brain the
investigator runs, and [`../deploy/gcp/README.md`](../deploy/gcp/README.md)
for the Cloud Run deployment (plus the Agent Engine note for the
investigator).
