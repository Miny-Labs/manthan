#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# deploy.sh — build + deploy the Manthan services to Cloud Run.
#
#   manthan-api           FastAPI gateway (uvicorn manthan_api.main:app), public
#   manthan-investigator  same image, command-overridden to the investigator
#                         agent service (runs investigations in-process),
#                         own service account, no CPU throttle
#   manthan-triage        same image, triage agent service (Stripe intake +
#                         route_event), own service account, public
#   manthan-advisor       same image, advisor agent service (ask /
#                         precheck_refund / …), own service account, public
#   manthan-worker        same image, deterministic workers (actor +
#                         prettifier), private, no CPU throttle
#   manthan-ui            Vite bundle behind Caddy, public
#
# Per-agent Agent Identity: each agent service runs as its OWN service
# account (created in README step 2; secret access granted per-secret in
# step 5). The identity is stamped into each card via A2A_SERVICE_ACCOUNT.
#
# Agent Engine note: the investigator is the one service that benefits from
# Vertex AI Agent Engine (`adk deploy agent_engine`) — see README "Agent
# runtime choice". This script deploys the Cloud Run fallback, which is the
# fully-working path today.
#
# Prerequisites (see deploy/gcp/README.md for the full runbook):
#   * APIs enabled: run, cloudbuild, artifactregistry, sqladmin,
#     secretmanager, cloudtrace
#   * Cloud SQL Postgres 16 instance + database created
#   * ./sql-migrate.sh applied
#   * ./secrets-bootstrap.sh run (creates manthan-{tenant}-gemini-api-key,
#     manthan-{tenant}-database-url, coral-{tenant}-* etc. and grants the
#     runtime SA per-secret accessor)
#
# Usage:
#   PROJECT_ID=my-project ./deploy.sh
#   PROJECT_ID=my-project REGION=europe-west1 TENANT=acme ./deploy.sh
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Parameters — override via env, e.g. PROJECT_ID=my-proj ./deploy.sh ─

# REQUIRED: your GCP project id. No default on purpose.
PROJECT_ID="${PROJECT_ID:-}"

# Cloud Run + Cloud SQL + Artifact Registry region.
REGION="${REGION:-us-central1}"

# Tenant slug — must match what secrets-bootstrap.sh was run with; also
# becomes the org slug in the Stripe webhook path (/webhooks/stripe/{org}).
TENANT="${TENANT:-acme}"

# Cloud SQL instance NAME (not connection string) created in the runbook.
SQL_INSTANCE="${SQL_INSTANCE:-manthan-pg}"

# Artifact Registry docker repo name.
AR_REPO="${AR_REPO:-manthan}"

# Runtime service account (created in the runbook; needs cloudsql.client
# + cloudtrace.agent project roles; secret access is per-secret).
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-manthan-runtime@${PROJECT_ID}.iam.gserviceaccount.com}"

# Per-agent service accounts (Agent Identity — one SA per macro agent;
# README step 2 creates them, step 5 grants each one per-secret access).
TRIAGE_SA="${TRIAGE_SA:-manthan-triage@${PROJECT_ID}.iam.gserviceaccount.com}"
INVESTIGATOR_SA="${INVESTIGATOR_SA:-manthan-investigator@${PROJECT_ID}.iam.gserviceaccount.com}"
ADVISOR_SA="${ADVISOR_SA:-manthan-advisor@${PROJECT_ID}.iam.gserviceaccount.com}"

# Optional: Clerk publishable key baked into the UI bundle at build time.
VITE_CLERK_PUBLISHABLE_KEY="${VITE_CLERK_PUBLISHABLE_KEY:-}"

# Image tag — defaults to the short git sha, falls back to a timestamp.
TAG="${TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d-%H%M%S)}"

# ── Derived values ─────────────────────────────────────────────────────

if [ -z "$PROJECT_ID" ]; then
    echo "ERROR: set PROJECT_ID, e.g. PROJECT_ID=my-project ./deploy.sh" >&2
    exit 64
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AR_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}"
API_IMAGE="${AR_BASE}/manthan-api:${TAG}"
UI_IMAGE="${AR_BASE}/manthan-ui:${TAG}"
SQL_CONN="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"

echo "project   : ${PROJECT_ID}"
echo "region    : ${REGION}"
echo "tenant    : ${TENANT}"
echo "tag       : ${TAG}"
echo "cloud sql : ${SQL_CONN}"
echo

# ── Helpers ────────────────────────────────────────────────────────────

secret_exists() {
    gcloud secrets describe "$1" --project "$PROJECT_ID" >/dev/null 2>&1
}

require_secret() {
    secret_exists "$1" || {
        echo "ERROR: required secret '$1' not found — run ./secrets-bootstrap.sh first" >&2
        exit 1
    }
}

# gcloud builds submit only supports a root-level Dockerfile with --tag,
# so we generate a Cloud Build config per image that (1) swaps in the
# per-Dockerfile context-ignore (the repo-root .dockerignore excludes
# manthan-ui — it predates this deploy) and (2) runs docker build with
# -f deploy/gcp/Dockerfile.* plus any --build-arg values.
build_image() {
    local image="$1" dockerfile="$2" ignorefile="$3"
    shift 3
    local tmpdir cfg
    tmpdir="${TMPDIR:-/tmp}"
    # BSD/macOS mktemp requires trailing Xs — create then rename to .yaml.
    cfg="$(mktemp "$tmpdir/cloudbuild.XXXXXX")"
    mv "$cfg" "${cfg}.yaml"
    cfg="${cfg}.yaml"
    {
        echo "steps:"
        echo "- name: gcr.io/cloud-builders/docker"
        echo "  entrypoint: bash"
        echo "  args:"
        echo "  - -c"
        echo "  - cp ${ignorefile} .dockerignore"
        echo "- name: gcr.io/cloud-builders/docker"
        echo "  args:"
        echo "  - build"
        echo "  - -f"
        echo "  - ${dockerfile}"
        local a
        for a in "$@"; do
            echo "  - --build-arg"
            echo "  - ${a}"
        done
        echo "  - -t"
        echo "  - ${image}"
        echo "  - ."
        echo "images:"
        echo "- ${image}"
    } > "$cfg"
    # Note: gcloud respects .gcloudignore / .gitignore, so gitignored
    # local secret files (agent/.env etc.) are NOT uploaded.
    gcloud builds submit "$REPO_ROOT" \
        --project "$PROJECT_ID" \
        --config "$cfg"
    rm -f "$cfg"
}

# ── 0. Ensure the Artifact Registry repo exists ───────────────────────

if ! gcloud artifacts repositories describe "$AR_REPO" \
        --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
    echo "==> creating Artifact Registry repo '${AR_REPO}' in ${REGION}"
    gcloud artifacts repositories create "$AR_REPO" \
        --project "$PROJECT_ID" \
        --location "$REGION" \
        --repository-format docker \
        --description "Manthan images"
fi

# ── 1. Secret-to-env wiring ────────────────────────────────────────────
# Required: Gemini key (AI Studio) + DATABASE_URL. Everything else is
# attached only if the secret exists, so a tenant with a partial source
# set still deploys cleanly.

GEMINI_SECRET="manthan-${TENANT}-gemini-api-key"
DB_SECRET="manthan-${TENANT}-database-url"
require_secret "$GEMINI_SECRET"
require_secret "$DB_SECRET"

SET_SECRETS="GOOGLE_API_KEY=${GEMINI_SECRET}:latest,DATABASE_URL=${DB_SECRET}:latest"

# Optional platform secrets (created by secrets-bootstrap.sh when present
# in the env file).
for pair in \
    "STRIPE_WEBHOOK_SECRET=manthan-${TENANT}-stripe-webhook-secret" \
    "EVENT_SIGNING_KEY=manthan-${TENANT}-event-signing-key" \
    "SOURCE_CONFIG_KEY=manthan-${TENANT}-source-config-key" \
    "CLERK_SECRET_KEY=manthan-${TENANT}-clerk-secret-key" \
; do
    name="${pair#*=}"
    if secret_exists "$name"; then
        SET_SECRETS="${SET_SECRETS},${pair}:latest"
    else
        echo "  (optional secret ${name} absent — skipping)"
    fi
done

# Coral source credentials — coral-{tenant}-{var-lowercased-hyphens}.
# The investigator agent spawns `coral mcp-stdio` per investigation, so
# it needs these in its env; the worker (coral-bootstrap source
# registration) and the API (/api/sources status) get the same set.
for pair in \
    "STRIPE_API_KEY=coral-${TENANT}-stripe-api-key" \
    "HUBSPOT_ACCESS_TOKEN=coral-${TENANT}-hubspot-access-token" \
    "INTERCOM_ACCESS_TOKEN=coral-${TENANT}-intercom-access-token" \
    "SLACK_TOKEN=coral-${TENANT}-slack-token" \
    "NOTION_API_KEY=coral-${TENANT}-notion-api-key" \
    "PAGERDUTY_API_TOKEN=coral-${TENANT}-pagerduty-api-token" \
    "DD_SITE=coral-${TENANT}-dd-site" \
    "DD_API_KEY=coral-${TENANT}-dd-api-key" \
    "DD_APPLICATION_KEY=coral-${TENANT}-dd-application-key" \
    "SENTRY_ORG=coral-${TENANT}-sentry-org" \
    "SENTRY_TOKEN=coral-${TENANT}-sentry-token" \
    "POSTHOG_API_BASE=coral-${TENANT}-posthog-api-base" \
    "POSTHOG_API_KEY=coral-${TENANT}-posthog-api-key" \
; do
    name="${pair#*=}"
    if secret_exists "$name"; then
        SET_SECRETS="${SET_SECRETS},${pair}:latest"
    else
        echo "  (coral secret ${name} absent — that source will be skipped)"
    fi
done

COMMON_ENV="GOOGLE_GENAI_USE_VERTEXAI=FALSE,CORAL_BINARY=/usr/local/bin/coral"

# ── 2. Build the API/worker image ─────────────────────────────────────

echo "==> building ${API_IMAGE}"
build_image "$API_IMAGE" "deploy/gcp/Dockerfile.api" "deploy/gcp/Dockerfile.api.dockerignore"

# ── 3. Deploy manthan-api ──────────────────────────────────────────────

echo "==> deploying manthan-api"
gcloud run deploy manthan-api \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "$API_IMAGE" \
    --service-account "$SERVICE_ACCOUNT" \
    --add-cloudsql-instances "$SQL_CONN" \
    --set-secrets "$SET_SECRETS" \
    --set-env-vars "$COMMON_ENV" \
    --port 8080 \
    --cpu 1 \
    --memory 1Gi \
    --min-instances 0 \
    --max-instances 3 \
    --concurrency 80 \
    --timeout 300 \
    --allow-unauthenticated

API_URL="$(gcloud run services describe manthan-api \
    --project "$PROJECT_ID" --region "$REGION" \
    --format 'value(status.url)')"
echo "    manthan-api -> ${API_URL}"

# A2A_PUBLIC_URL: the base URL stamped into the AgentCard
# (/.well-known/agent-card.json) so other agents can call back in.
gcloud run services update manthan-api \
    --project "$PROJECT_ID" --region "$REGION" \
    --update-env-vars "A2A_PUBLIC_URL=${API_URL}"

# ── 3b. Deploy the three agent services (same image, uvicorn overrides) ─
# Each is its own Cloud Run service + its own service account (per-agent
# Agent Identity). Deploy order matters: investigator first (triage and
# the gateway need its URL), then triage, then advisor.

# manthan-investigator — runs investigations IN-PROCESS as background
# tasks after acking the A2A call, so:
#   * --no-cpu-throttling : CPU stays allocated after the response
#   * min-instances 1     : the instance must outlive the request
# NOTE: deployed unauthenticated for the hackathon demo (the A2A card is
# public by design). For production flip to --no-allow-unauthenticated
# and put ID-token auth on the triage->investigator hop.
echo "==> deploying manthan-investigator"
gcloud run deploy manthan-investigator \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "$API_IMAGE" \
    --command "/bin/bash" \
    --args "-c,/usr/local/bin/coral-bootstrap.sh; exec uvicorn manthan_api.agents.investigator:app --host 0.0.0.0 --port 8080" \
    --service-account "$INVESTIGATOR_SA" \
    --add-cloudsql-instances "$SQL_CONN" \
    --set-secrets "$SET_SECRETS" \
    --set-env-vars "${COMMON_ENV},A2A_SERVICE_ACCOUNT=${INVESTIGATOR_SA}" \
    --port 8080 \
    --cpu 2 \
    --memory 2Gi \
    --min-instances "${INVESTIGATOR_MIN_INSTANCES:-0}" \
    --max-instances 2 \
    --concurrency 10 \
    --timeout 600 \
    --no-cpu-throttling \
    --allow-unauthenticated

INVESTIGATOR_URL="$(gcloud run services describe manthan-investigator \
    --project "$PROJECT_ID" --region "$REGION" \
    --format 'value(status.url)')"
echo "    manthan-investigator -> ${INVESTIGATOR_URL}"
gcloud run services update manthan-investigator \
    --project "$PROJECT_ID" --region "$REGION" \
    --update-env-vars "A2A_PUBLIC_URL=${INVESTIGATOR_URL}"

# manthan-triage — stateless event intake (Stripe webhooks must reach it,
# so it is public). No Cloud SQL attachment: it forwards to the
# investigator over A2A and touches no tables itself.
echo "==> deploying manthan-triage"
gcloud run deploy manthan-triage \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "$API_IMAGE" \
    --command "uvicorn" \
    --args "manthan_api.agents.triage:app,--host,0.0.0.0,--port,8080" \
    --service-account "$TRIAGE_SA" \
    --set-secrets "$SET_SECRETS" \
    --set-env-vars "${COMMON_ENV},A2A_SERVICE_ACCOUNT=${TRIAGE_SA},INVESTIGATOR_A2A_URL=${INVESTIGATOR_URL}" \
    --port 8080 \
    --cpu 1 \
    --memory 512Mi \
    --min-instances 0 \
    --max-instances 3 \
    --concurrency 80 \
    --timeout 60 \
    --allow-unauthenticated

TRIAGE_URL="$(gcloud run services describe manthan-triage \
    --project "$PROJECT_ID" --region "$REGION" \
    --format 'value(status.url)')"
echo "    manthan-triage -> ${TRIAGE_URL}"
gcloud run services update manthan-triage \
    --project "$PROJECT_ID" --region "$REGION" \
    --update-env-vars "A2A_PUBLIC_URL=${TRIAGE_URL}"

# manthan-advisor — the public conversational A2A face (ask /
# precheck_refund / get_customer_history / dispute_exposure /
# contribute_evidence + the 6 reads). Needs the DB + the Gemini key.
echo "==> deploying manthan-advisor"
gcloud run deploy manthan-advisor \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "$API_IMAGE" \
    --command "uvicorn" \
    --args "manthan_api.agents.advisor:app,--host,0.0.0.0,--port,8080" \
    --service-account "$ADVISOR_SA" \
    --add-cloudsql-instances "$SQL_CONN" \
    --set-secrets "$SET_SECRETS" \
    --set-env-vars "${COMMON_ENV},A2A_SERVICE_ACCOUNT=${ADVISOR_SA},INVESTIGATOR_A2A_URL=${INVESTIGATOR_URL}" \
    --port 8080 \
    --cpu 1 \
    --memory 1Gi \
    --min-instances 0 \
    --max-instances 3 \
    --concurrency 40 \
    --timeout 120 \
    --allow-unauthenticated

ADVISOR_URL="$(gcloud run services describe manthan-advisor \
    --project "$PROJECT_ID" --region "$REGION" \
    --format 'value(status.url)')"
echo "    manthan-advisor -> ${ADVISOR_URL}"
gcloud run services update manthan-advisor \
    --project "$PROJECT_ID" --region "$REGION" \
    --update-env-vars "A2A_PUBLIC_URL=${ADVISOR_URL}"

# Wire the gateway: back-compat Stripe webhooks forward to triage, the
# gateway card points conversational callers at the advisor, and
# investigate_dispute via the gateway routes to the investigator.
gcloud run services update manthan-api \
    --project "$PROJECT_ID" --region "$REGION" \
    --update-env-vars "TRIAGE_A2A_URL=${TRIAGE_URL},ADVISOR_A2A_URL=${ADVISOR_URL},INVESTIGATOR_A2A_URL=${INVESTIGATOR_URL}"

# ── 4. Deploy manthan-worker (same image, command override) ───────────
# Deterministic workers ONLY (actor + prettifier). The investigate worker
# is retired — investigations run inside manthan-investigator above.
# * --no-cpu-throttling: the actor does background writes outside of
#   request handling; CPU must stay allocated between requests.
# * max 1 instance: the actor's queue is single-instance PG polling.
#   Run ./pubsub-setup.sh and re-deploy with more instances only after
#   switching the bus to Pub/Sub. Default min 0 = budget profile (see
#   the Cloud Scheduler wake-ping below); WORKER_MIN_INSTANCES=1 for
#   always-on production.

echo "==> deploying manthan-worker"
gcloud run deploy manthan-worker \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "$API_IMAGE" \
    --command "/usr/local/bin/worker-entrypoint.sh" \
    --service-account "$SERVICE_ACCOUNT" \
    --add-cloudsql-instances "$SQL_CONN" \
    --set-secrets "$SET_SECRETS" \
    --set-env-vars "${COMMON_ENV},A2A_PUBLIC_URL=${API_URL}" \
    --port 8080 \
    --cpu 2 \
    --memory 2Gi \
    --min-instances "${WORKER_MIN_INSTANCES:-0}" \
    --max-instances 1 \
    --no-cpu-throttling \
    --no-allow-unauthenticated

# Budget profile (WORKER_MIN_INSTANCES=0, the default): the actor polls
# Postgres, so a scaled-to-zero worker needs a wake signal. A Cloud
# Scheduler ping every 5 minutes wakes it; instance billing means you pay
# for minutes of actual wakefulness instead of an always-on instance
# (~$30/mo -> <$1/mo). Set WORKER_MIN_INSTANCES=1 for production tenants.
if [ "${WORKER_MIN_INSTANCES:-0}" = "0" ]; then
    echo "==> budget profile: Cloud Scheduler wake-ping for manthan-worker"
    gcloud services enable cloudscheduler.googleapis.com --project "$PROJECT_ID"
    WORKER_URL="$(gcloud run services describe manthan-worker \
        --project "$PROJECT_ID" --region "$REGION" \
        --format 'value(status.url)')"
    gcloud run services add-iam-policy-binding manthan-worker \
        --project "$PROJECT_ID" --region "$REGION" \
        --member "serviceAccount:${SERVICE_ACCOUNT}" \
        --role roles/run.invoker --quiet
    gcloud scheduler jobs create http manthan-worker-wake \
        --project "$PROJECT_ID" --location "$REGION" \
        --schedule "*/5 * * * *" \
        --uri "${WORKER_URL}/healthz" \
        --http-method GET \
        --oidc-service-account-email "$SERVICE_ACCOUNT" \
        --quiet 2>/dev/null || echo "    (scheduler job exists)"
fi

# ── 5. Build + deploy manthan-ui ───────────────────────────────────────
# Built AFTER the API so the API URL can be baked into the bundle
# (VITE_MANTHAN_API_URL — see Dockerfile.ui for how the UI resolves it).

echo "==> building ${UI_IMAGE}"
UI_BUILD_ARGS=("VITE_MANTHAN_API_URL=${API_URL}" "VITE_MANTHAN_DEV_ORG=${TENANT}")
if [ -n "$VITE_CLERK_PUBLISHABLE_KEY" ]; then
    UI_BUILD_ARGS+=("VITE_CLERK_PUBLISHABLE_KEY=${VITE_CLERK_PUBLISHABLE_KEY}")
fi
build_image "$UI_IMAGE" "deploy/gcp/Dockerfile.ui" "deploy/gcp/Dockerfile.ui.dockerignore" "${UI_BUILD_ARGS[@]}"

echo "==> deploying manthan-ui"
gcloud run deploy manthan-ui \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "$UI_IMAGE" \
    --port 8080 \
    --cpu 1 \
    --memory 256Mi \
    --min-instances 0 \
    --max-instances 2 \
    --allow-unauthenticated

UI_URL="$(gcloud run services describe manthan-ui \
    --project "$PROJECT_ID" --region "$REGION" \
    --format 'value(status.url)')"
echo "    manthan-ui -> ${UI_URL}"

# ── 6. Close the CORS loop ─────────────────────────────────────────────
# manthan_api/config.py allows exactly one browser origin (WEB_APP_ORIGIN).

gcloud run services update manthan-api \
    --project "$PROJECT_ID" --region "$REGION" \
    --update-env-vars "WEB_APP_ORIGIN=${UI_URL}"

# ── Done ───────────────────────────────────────────────────────────────

cat <<EOF

──────────────────────────────────────────────────────────────────────
Deployed.

  API (gateway)     : ${API_URL}
  UI                : ${UI_URL}
  Triage agent      : ${TRIAGE_URL}
      card          : ${TRIAGE_URL}/.well-known/agent-card.json
  Investigator agent: ${INVESTIGATOR_URL}
      card          : ${INVESTIGATOR_URL}/.well-known/agent-card.json
  Advisor agent     : ${ADVISOR_URL}
      card          : ${ADVISOR_URL}/.well-known/agent-card.json
  Gateway card      : ${API_URL}/.well-known/agent-card.json
  Health            : ${API_URL}/healthz

Next (see README.md sections 8-10):
  * Stripe webhook endpoint (CANONICAL — triage agent):
        ${TRIAGE_URL}/webhooks/stripe
    (back-compat: ${API_URL}/webhooks/stripe/${TENANT} still works and
     forwards to triage)
    events: charge.dispute.created, charge.dispute.funds_withdrawn,
            charge.dispute.closed, radar.early_fraud_warning.created,
            invoice.payment_failed
    then store the signing secret:
      ./secrets-bootstrap.sh ${TENANT} <env-file-with-STRIPE_WEBHOOK_SECRET> ${TRIAGE_SA}
      and re-run this script (or gcloud run services update manthan-triage
      --set-secrets ... ) to attach it.
  * Optional multi-instance bus for the actor: ./pubsub-setup.sh
  * Preferred investigator runtime: Agent Engine (see README "Agent
    runtime choice") — this script deployed the Cloud Run fallback.
──────────────────────────────────────────────────────────────────────
EOF
