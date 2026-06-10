# Manthan on GCP — end-to-end runbook

Everything in this directory is **files-only scaffolding**: nothing here
has been executed against a real project. Follow the numbered steps with
a real `PROJECT_ID` and it deploys the full stack:

| Service | Image | What it runs | Exposure |
|---|---|---|---|
| `manthan-api` | `Dockerfile.api` | `uvicorn manthan_api.main:app` (port 8080) — gateway + UI API | public |
| `manthan-triage` | `Dockerfile.api` (same image) | `uvicorn manthan_api.agents.triage:app` — Stripe intake + `route_event` → investigator over A2A | public (Stripe must reach it), own SA `manthan-triage@…` |
| `manthan-investigator` | `Dockerfile.api` (same image) | `uvicorn manthan_api.agents.investigator:app` — `investigate_dispute` A2A skill; runs the ADK investigation **in-process** and writes events/projections itself (`services.case_store`) | public card (lock down for prod), own SA `manthan-investigator@…`, `--no-cpu-throttling`, min 1 |
| `manthan-advisor` | `Dockerfile.api` (same image) | `uvicorn manthan_api.agents.advisor:app` — `ask` / `precheck_refund` / `get_customer_history` / `dispute_exposure` / `contribute_evidence` + 6 reads | public, own SA `manthan-advisor@…` |
| `manthan-worker` | `Dockerfile.api` (same image) | `worker-entrypoint.sh` → `python -m manthan_api.workers.main` (actor + prettifier — **deterministic only**; the investigate worker is retired) | private, `--no-cpu-throttling`, 1 instance |
| `manthan-ui` | `Dockerfile.ui` | Caddy serving the Vite bundle (SPA fallback, no proxying) | public |

Event flow (no NOTIFY pipeline between agents anymore):

```
Stripe ─▶ manthan-triage ──A2A investigate_dispute──▶ manthan-investigator
                                                        │ (in-process ADK run)
                                                        ▼ writes events/findings/brief
other agents ─▶ manthan-advisor ──reads/answers──▶  Cloud SQL ◀── manthan-api (UI reads)
                                                        ▲
                                     manthan-worker (actor) drains approved actions
```

Files:

- `Dockerfile.api` — python:3.12-slim multi-stage (uv), installs `agent/` +
  `manthan-api/` (editable path dep preserved), bakes the **Coral v0.4.2**
  linux x86_64 binary (asset `coral-x86_64-unknown-linux-gnu.tar.gz`,
  sha256-pinned) into `/usr/local/bin/coral`.
- `Dockerfile.ui` + `Caddyfile` — node:20 build → caddy:2-alpine. The API
  base is **baked at build time** via `VITE_MANTHAN_API_URL`
  (resolved in `manthan-ui/src/lib/api.ts`, `useInboxStream.ts`,
  `useCaseEvents.ts`; empty default = same-origin, which does NOT work
  across Cloud Run origins — `deploy.sh` handles this ordering).
- `coral-bootstrap.sh` — registers Coral sources at worker start; skips
  sources with missing credentials; always exits 0.
- `worker-entrypoint.sh` — Cloud Run command override for the worker
  (coral bootstrap + `$PORT` health shim + the deterministic workers,
  actor + prettifier).
- `secrets-bootstrap.sh` — env file → Secret Manager with per-secret IAM.
- `sql-migrate.sh` — applies `manthan-api/schema/*.sql` in order via psql.
- `deploy.sh` — 2 × `gcloud builds submit` + 6 × `gcloud run deploy` (api gateway, investigator, triage, advisor, worker, ui).
- `Dockerfile.api.dockerignore` / `Dockerfile.ui.dockerignore` —
  per-image context ignores (deploy.sh swaps them in before each docker
  build step; the repo-root `.dockerignore` excludes `manthan-ui` and
  would break the UI build).
- `pubsub-setup.sh` — optional multi-instance upgrade path (default bus
  stays single-instance PG LISTEN/NOTIFY).

Conventions used throughout (must match `secrets-bootstrap.sh` output):

- Coral source secrets: `coral-{tenant}-{env-var-lowercased-hyphens}`
  (e.g. `STRIPE_API_KEY` → `coral-acme-stripe-api-key`)
- Gemini key: `manthan-{tenant}-gemini-api-key` (env `GOOGLE_API_KEY`)
- Platform secrets: `manthan-{tenant}-database-url`,
  `manthan-{tenant}-stripe-webhook-secret`, …
- Models (AI Studio, `GOOGLE_GENAI_USE_VERTEXAI=FALSE`):
  `gemini-3.1-pro-preview` / `gemini-3.5-flash` / `gemini-3.1-flash-lite`.

---

## 0. Prerequisites

- `gcloud` CLI authenticated (`gcloud auth login`), a project with billing.
- `psql` + [`cloud-sql-proxy`](https://cloud.google.com/sql/docs/postgres/connect-auth-proxy) locally (for migrations).
- `stripe` CLI (for the smoke test).
- A tenant env file (e.g. a copy of `agent/.env`) holding the AI Studio
  key as `GOOGLE_API_KEY` plus whichever Coral source credentials this
  tenant has (`STRIPE_API_KEY`, `SLACK_TOKEN`, …).

```bash
export PROJECT_ID=your-project-id        # <-- the only thing you must change
export REGION=us-central1
export TENANT=acme
export SERVICE_ACCOUNT="manthan-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud config set project "$PROJECT_ID"
```

## 1. Enable APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  cloudtrace.googleapis.com
```

Note: `aiplatform.googleapis.com` (Vertex) is **deliberately not needed**
— Gemini is called through AI Studio (`generativelanguage.googleapis.com`)
with a plain `GOOGLE_API_KEY`, which is not a GCP service API you enable.

## 2. Service accounts (per-agent Agent Identity)

One runtime SA for the gateway/worker/UI plumbing, plus **one SA per macro
agent** — that per-agent identity is what each AgentCard's
`manthanIdentity.serviceAccount` advertises and what the roster UI shows.

```bash
gcloud iam service-accounts create manthan-runtime \
  --display-name "Manthan runtime (api + worker)"
gcloud iam service-accounts create manthan-triage \
  --display-name "Manthan triage agent"
gcloud iam service-accounts create manthan-investigator \
  --display-name "Manthan investigator agent"
gcloud iam service-accounts create manthan-advisor \
  --display-name "Manthan advisor agent"

# Project-level roles: Cloud SQL connector + trace export for every SA
# that touches the DB / emits traces (triage needs neither SQL nor trace,
# but cloudtrace.agent is harmless and useful once its hop is traced).
for SA in manthan-runtime manthan-investigator manthan-advisor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role roles/cloudsql.client
done
for SA in manthan-runtime manthan-triage manthan-investigator manthan-advisor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role roles/cloudtrace.agent
done
```

Secret access is granted **per-secret** in step 5 — do not grant
project-level `secretmanager.secretAccessor`. Run `secrets-bootstrap.sh`
once per service account (runtime + the three agents) so each agent can
read only the secrets it is wired to.

## 3. Cloud SQL (Postgres 16)

```bash
gcloud sql instances create manthan-pg \
  --database-version POSTGRES_16 \
  --edition enterprise \
  --tier db-custom-1-3840 \
  --region "$REGION" \
  --storage-size 10GB \
  --storage-auto-increase

gcloud sql databases create manthan --instance manthan-pg

# App user (password auth — simplest path; see "not automated" for IAM auth).
gcloud sql users create manthan --instance manthan-pg --password 'CHANGE-ME-STRONG'

# Optional but recommended: an IAM-auth user for humans/ops.
gcloud sql users create "$SERVICE_ACCOUNT" \
  --instance manthan-pg --type cloud_iam_service_account
```

Two `DATABASE_URL` forms are used:

- **Migrations (local, via proxy):**
  `postgresql://manthan:CHANGE-ME-STRONG@127.0.0.1:5432/manthan`
- **Cloud Run (unix socket — this is what goes into Secret Manager):**
  `postgresql://manthan:CHANGE-ME-STRONG@/manthan?host=/cloudsql/PROJECT_ID:REGION:manthan-pg`

## 4. Apply the schema

```bash
# Terminal A:
cloud-sql-proxy --port 5432 "${PROJECT_ID}:${REGION}:manthan-pg"

# Terminal B:
DATABASE_URL="postgresql://manthan:CHANGE-ME-STRONG@127.0.0.1:5432/manthan" \
  ./sql-migrate.sh
```

Applies `manthan-api/schema/001_initial.sql` … `005_auth_signups.sql`
in filename order with `ON_ERROR_STOP`.

## 5. Secrets

Put the **Cloud Run form** of `DATABASE_URL` (unix socket, step 3) into
your tenant env file alongside `GOOGLE_API_KEY` and the Coral source
credentials, then:

```bash
./secrets-bootstrap.sh "$TENANT" /path/to/tenant.env "$SERVICE_ACCOUNT"
```

Creates/updates each known var and binds
`roles/secretmanager.secretAccessor` for the runtime SA on each secret
individually.

## 6. Deploy

```bash
PROJECT_ID="$PROJECT_ID" REGION="$REGION" TENANT="$TENANT" \
VITE_CLERK_PUBLISHABLE_KEY=pk_live_... \
  ./deploy.sh
```

What it does, in order: ensures the Artifact Registry repo → builds the
api/worker image → deploys `manthan-api` (Cloud SQL attached,
`--set-secrets` for `GOOGLE_API_KEY`, `DATABASE_URL`, the coral source
secrets and any optional platform secrets) → sets `A2A_PUBLIC_URL` from
the live api URL → deploys the **three agent services** from the same
image with uvicorn `--command/--args` overrides (`manthan-investigator`
first with `--no-cpu-throttling` + min 1 instance because investigations
run in-process past the A2A response; then `manthan-triage` with
`INVESTIGATOR_A2A_URL` wired; then `manthan-advisor`) → wires the gateway
(`TRIAGE_A2A_URL`, `ADVISOR_A2A_URL`, `INVESTIGATOR_A2A_URL` on
`manthan-api`) → deploys `manthan-worker` (actor + prettifier only,
`--no-cpu-throttling`, pinned to exactly 1 instance for the PG
LISTEN/NOTIFY action queue) → builds the UI **with the api URL baked in**
→ deploys `manthan-ui` → points the api's `WEB_APP_ORIGIN` (CORS) at the
UI URL.

### Agent runtime choice: Agent Engine (preferred) vs Cloud Run (fallback)

The investigator is a pure ADK agent system, which makes **Vertex AI
Agent Engine the preferred runtime** for it (managed sessions, the
console's Traces/Sessions/Memory tabs, Agent Registry residency):

```bash
# from agent/ — requires GOOGLE_GENAI_USE_VERTEXAI=TRUE + a staging bucket
pip install "google-cloud-aiplatform[adk,agent_engines]"
adk deploy agent_engine \
  --project "$PROJECT_ID" --region "$REGION" \
  --staging_bucket "gs://${PROJECT_ID}-agent-staging" \
  src/manthan_agent
```

Two integration notes before flipping to it (why Cloud Run is the
*working* path today and stays as the fallback):

1. **Coral transport** — on Agent Engine there is no sidecar process, so
   the stdio `coral mcp-stdio` subprocess must become a streamable-HTTP
   MCP endpoint (Coral 0.4.2 supports it; run Coral as its own Cloud Run
   service and point the tools at its URL).
2. **Event projections** — the in-process runner writes to Cloud SQL via
   `services.case_store`; on Agent Engine that write path needs the
   public-IP Cloud SQL connector or a small ingest endpoint on
   `manthan-api`.

Triage and advisor stay on Cloud Run either way — they are thin
FastAPI/A2A surfaces, not ADK loops.

## 7. Configure the Stripe webhook

In the Stripe Dashboard (Developers → Webhooks → Add endpoint), or via CLI:

- **Endpoint URL (canonical — the triage agent):**
  `https://<manthan-triage-url>/webhooks/stripe`
  (back-compat: `https://<manthan-api-url>/webhooks/stripe/<TENANT>` still
  works — the gateway forwards to triage when `TRIAGE_A2A_URL` is set,
  else opens the case directly)
- **Events (exactly these five):**
  1. `charge.dispute.created` (primary trigger)
  2. `charge.dispute.funds_withdrawn`
  3. `charge.dispute.closed`
  4. `radar.early_fraud_warning.created`
  5. `invoice.payment_failed`

Then store the endpoint's signing secret (`whsec_…`) in Secret Manager
and attach it (to triage, the canonical receiver — and to the api if you
keep the back-compat endpoint registered with Stripe too):

```bash
echo "STRIPE_WEBHOOK_SECRET=whsec_..." > /tmp/whsec.env
./secrets-bootstrap.sh "$TENANT" /tmp/whsec.env "manthan-triage@${PROJECT_ID}.iam.gserviceaccount.com"
rm /tmp/whsec.env

gcloud run services update manthan-triage --region "$REGION" \
  --set-secrets "STRIPE_WEBHOOK_SECRET=manthan-${TENANT}-stripe-webhook-secret:latest"
# (re-running ./deploy.sh also picks it up now that the secret exists)
```

## 8. Point a domain (optional)

```bash
gcloud run domain-mappings create --service manthan-ui \
  --region "$REGION" --domain app.yourdomain.com
gcloud run domain-mappings create --service manthan-api \
  --region "$REGION" --domain api.yourdomain.com
```

Add the DNS records gcloud prints. If you map the API to a custom
domain, re-run the UI build/deploy with the new
`VITE_MANTHAN_API_URL`, update `WEB_APP_ORIGIN`, and update
`A2A_PUBLIC_URL` so the agent card advertises the right base.

## 9. Smoke tests

```bash
API_URL="$(gcloud run services describe manthan-api --region "$REGION" --format 'value(status.url)')"
TRIAGE_URL="$(gcloud run services describe manthan-triage --region "$REGION" --format 'value(status.url)')"
INVESTIGATOR_URL="$(gcloud run services describe manthan-investigator --region "$REGION" --format 'value(status.url)')"
ADVISOR_URL="$(gcloud run services describe manthan-advisor --region "$REGION" --format 'value(status.url)')"

# Liveness
curl -fsS "$API_URL/healthz"

# Each agent serves its own card with its own identity:
curl -fsS "$TRIAGE_URL/.well-known/agent-card.json" | python3 -m json.tool        # manthan-triage, route_event
curl -fsS "$INVESTIGATOR_URL/.well-known/agent-card.json" | python3 -m json.tool  # manthan-investigator, investigate_dispute + 6 reads
curl -fsS "$ADVISOR_URL/.well-known/agent-card.json" | python3 -m json.tool       # manthan-advisor, ask/precheck_refund/… + 6 reads
curl -fsS "$API_URL/.well-known/agent-card.json" | python3 -m json.tool           # gateway (aggregate card)

# Ask the advisor something over A2A:
curl -fsS -X POST "$ADVISOR_URL/a2a" -H 'content-type: application/json' -d '{
  "jsonrpc": "2.0", "id": 1, "method": "message/send",
  "params": {"skill": "dispute_exposure", "args": {}}
}' | python3 -m json.tool

# `ask` round-trip — a grounded, cited answer (pass a real case_id from
# the inbox to scope it to one case; without it the answer is cross-case):
curl -fsS -X POST "$ADVISOR_URL/a2a" -H 'content-type: application/json' -d '{
  "jsonrpc": "2.0", "id": 2, "method": "message/send",
  "params": {"skill": "ask", "args": {"question": "What is our current dispute exposure and which case should I look at first?"}}
}' | python3 -m json.tool

# Fire a test dispute at the deployed webhook (uses your Stripe TEST key):
stripe trigger charge.dispute.created
# If your webhook endpoint is in test mode, the event arrives directly.
# Alternatively forward events from your machine:
#   stripe listen --forward-to "$TRIAGE_URL/webhooks/stripe"
#   stripe trigger charge.dispute.created

# Then watch the investigator run the case (in-process, same service):
gcloud run services logs read manthan-investigator --region "$REGION" --limit 50
```

Expected: webhook 200 at triage → A2A `investigate_dispute` to the
investigator → a case appears (`investigating` → `awaiting_approval`) in
the UI inbox, with events/findings/brief written by the investigator
itself (no worker hop).

## 10. Optional: Pub/Sub multi-instance upgrade

```bash
PROJECT_ID="$PROJECT_ID" ./pubsub-setup.sh
```

Creates the `manthan-events` topic + an OIDC-authenticated push
subscription template to `/webhooks/pubsub`. **The default deployment
does not use it** — see the header of `pubsub-setup.sh` for the three
cutover steps (handler, publisher, worker max-instances).

---

## What is NOT automated (honest list)

- **Running anything.** No gcloud command in this directory has been
  executed; everything was validated with `bash -n` + container-free
  inspection only. Asset name + sha256 of the Coral binary were
  verified against the real GitHub release.
- **The `/webhooks/pubsub` handler does not exist yet** — `pubsub-setup.sh`
  is a template for the multi-instance upgrade path only.
- **Agent Engine deployment** — documented in section 6 ("Agent runtime
  choice") but not scripted; `deploy.sh` ships the Cloud Run fallback for
  the investigator.
- **A2A auth between the agents** — the three agent services deploy
  `--allow-unauthenticated` for the demo; production should flip triage→
  investigator (and gateway→agents) to ID-token auth between their
  service accounts.
- **Clerk** — creating the Clerk app, `CLERK_SECRET_KEY` /
  `VITE_CLERK_PUBLISHABLE_KEY` issuance, and JWT verification key setup.
- **Stripe webhook creation** — step 7 is manual (dashboard or CLI);
  only the secret storage is scripted.
- **Cloud SQL IAM database auth** for the app user — the runbook uses
  password auth; switching the app to IAM auth needs a `DATABASE_URL`
  change + token plumbing.
- **A migrations ledger** — `sql-migrate.sh` applies files in order but
  keeps no `schema_migrations` table; re-runs against a migrated DB fail
  fast instead of skipping.
- **Local classic-builder docker builds** — `gcloud builds submit`
  honors the root `.gitignore` (local `agent/.env` etc. are never
  uploaded), and `deploy.sh` swaps in the per-image
  `Dockerfile.{api,ui}.dockerignore` (which exclude `**/.env*`) before
  each Cloud Build docker step. Local builds with BuildKit (Docker 23+
  default) pick those files up automatically; a local build with the
  *legacy* builder (`DOCKER_BUILDKIT=0`) would fall back to the repo-root
  `.dockerignore`, which excludes `manthan-ui` (breaking the UI build)
  and does NOT exclude nested `.env` files — don't do that.
- **Secret rotation** — the local test keys that appeared during
  development must be rotated before any public demo (see
  the SUBMISSION.md checklist).
- **Coral source verification** — `coral-bootstrap.sh` registers sources
  but does not validate that each credential actually works; first
  investigation surfaces failures.
- **Budget/alerting, uptime checks, log-based metrics** — none configured.
