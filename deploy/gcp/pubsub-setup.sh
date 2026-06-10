#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# pubsub-setup.sh — OPTIONAL: Pub/Sub upgrade path for multi-instance.
#
# DEFAULT ARCHITECTURE (no Pub/Sub needed):
#   The API writes events to Postgres and pg_notify()'s the
#   "manthan_event" channel; the single manthan-worker instance holds a
#   LISTEN connection (manthan-api/src/manthan_api/workers/investigate.py).
#   This is why deploy.sh pins the worker at min=max=1 instance.
#
# WHEN TO RUN THIS SCRIPT:
#   Only when you need >1 worker instance (or cross-region fan-out).
#   PG NOTIFY delivers to every listener — multiple workers would each
#   pick up the same case. Pub/Sub gives competing consumers + retries.
#
# WHAT IT CREATES:
#   * topic 'manthan-events'
#   * a PUSH subscription template pointed at the API's
#     /webhooks/pubsub endpoint, authenticated with an OIDC token from
#     the runtime service account.
#
# WHAT IT DOES NOT DO (deliberately — this is an upgrade *template*):
#   * the API does not yet expose POST /webhooks/pubsub — add a handler
#     in manthan-api/src/manthan_api/api/webhooks.py that verifies the
#     OIDC token and translates the Pub/Sub envelope into the same
#     internal event the NOTIFY path produces;
#   * the event-emitting code still calls pg_notify() — publish to the
#     topic instead (or in addition) when cutting over;
#   * after cutover, raise manthan-worker's max-instances in deploy.sh.
#
# Usage:
#   PROJECT_ID=my-project ./pubsub-setup.sh
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-us-central1}"
TOPIC="${TOPIC:-manthan-events}"
SUBSCRIPTION="${SUBSCRIPTION:-manthan-events-push}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-manthan-runtime@${PROJECT_ID}.iam.gserviceaccount.com}"

if [ -z "$PROJECT_ID" ]; then
    echo "ERROR: set PROJECT_ID, e.g. PROJECT_ID=my-project ./pubsub-setup.sh" >&2
    exit 64
fi

# Push target: the deployed API service URL + the pubsub webhook path.
# Defaults to the live manthan-api URL; override with API_URL=... .
API_URL="${API_URL:-$(gcloud run services describe manthan-api \
    --project "$PROJECT_ID" --region "$REGION" \
    --format 'value(status.url)' 2>/dev/null || true)}"

if [ -z "$API_URL" ]; then
    echo "ERROR: could not resolve the manthan-api URL — deploy it first or pass API_URL=..." >&2
    exit 1
fi

PUSH_ENDPOINT="${API_URL}/webhooks/pubsub"

echo "topic        : ${TOPIC}"
echo "subscription : ${SUBSCRIPTION}"
echo "push to      : ${PUSH_ENDPOINT}"
echo

# ── Topic ──────────────────────────────────────────────────────────────
if gcloud pubsub topics describe "$TOPIC" --project "$PROJECT_ID" >/dev/null 2>&1; then
    echo "topic '${TOPIC}' already exists"
else
    gcloud pubsub topics create "$TOPIC" --project "$PROJECT_ID"
    echo "created topic '${TOPIC}'"
fi

# ── Push subscription (OIDC-authenticated) ─────────────────────────────
# The push request carries an OIDC identity token minted for
# SERVICE_ACCOUNT; the /webhooks/pubsub handler must verify it.
if gcloud pubsub subscriptions describe "$SUBSCRIPTION" --project "$PROJECT_ID" >/dev/null 2>&1; then
    echo "subscription '${SUBSCRIPTION}' already exists — updating push config"
    gcloud pubsub subscriptions modify-push-config "$SUBSCRIPTION" \
        --project "$PROJECT_ID" \
        --push-endpoint "$PUSH_ENDPOINT" \
        --push-auth-service-account "$SERVICE_ACCOUNT"
else
    gcloud pubsub subscriptions create "$SUBSCRIPTION" \
        --project "$PROJECT_ID" \
        --topic "$TOPIC" \
        --push-endpoint "$PUSH_ENDPOINT" \
        --push-auth-service-account "$SERVICE_ACCOUNT" \
        --ack-deadline 600 \
        --min-retry-delay 10s \
        --max-retry-delay 600s
    echo "created push subscription '${SUBSCRIPTION}'"
fi

cat <<EOF

Pub/Sub scaffolding ready. REMINDER — this is the multi-instance
upgrade path, NOT active yet:
  1. add a POST /webhooks/pubsub handler to manthan-api (verify the
     OIDC token, unwrap the Pub/Sub message envelope),
  2. publish events to '${TOPIC}' where the API currently pg_notify()'s,
  3. raise manthan-worker max-instances in deploy.sh.
Until then the single-instance PG LISTEN/NOTIFY default keeps working.
EOF
