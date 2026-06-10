#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# secrets-bootstrap.sh — push tenant credentials into Secret Manager.
#
# Usage:
#   ./secrets-bootstrap.sh <TENANT> <ENV_FILE> <SERVICE_ACCOUNT> [PROJECT_ID]
#
#   TENANT           tenant slug, e.g. "acme" (used in secret names)
#   ENV_FILE         path to a KEY=value file (e.g. agent/.env) — only
#                    KNOWN vars found in it are uploaded; everything
#                    else is ignored
#   SERVICE_ACCOUNT  runtime SA email, e.g.
#                    manthan-runtime@PROJECT.iam.gserviceaccount.com
#   PROJECT_ID       optional; defaults to the active gcloud project
#
# Behaviour:
#   * creates the secret if missing, else adds a new version
#   * binds roles/secretmanager.secretAccessor for SERVICE_ACCOUNT on
#     EACH secret individually (per-secret IAM — NOT project-level)
#
# Naming convention:
#   Coral source vars  -> coral-{tenant}-{env-var-lowercased-hyphens}
#                         e.g. STRIPE_API_KEY -> coral-acme-stripe-api-key
#   Gemini key         -> GOOGLE_API_KEY -> manthan-{tenant}-gemini-api-key
#   Platform vars      -> manthan-{tenant}-{env-var-lowercased-hyphens}
#                         (DATABASE_URL, STRIPE_WEBHOOK_SECRET,
#                          EVENT_SIGNING_KEY, SOURCE_CONFIG_KEY,
#                          CLERK_SECRET_KEY)
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

usage() {
    echo "usage: $0 <TENANT> <ENV_FILE> <SERVICE_ACCOUNT> [PROJECT_ID]" >&2
    exit 64
}

[ $# -ge 3 ] || usage

TENANT="$1"
ENV_FILE="$2"
SERVICE_ACCOUNT="$3"
PROJECT_ID="${4:-$(gcloud config get-value project 2>/dev/null)}"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: env file not found: $ENV_FILE" >&2
    exit 66
fi
if [ -z "$PROJECT_ID" ]; then
    echo "ERROR: no PROJECT_ID given and no active gcloud project" >&2
    exit 64
fi

# Coral source credentials (keys-only auth), per-source map mirrored in
# coral-bootstrap.sh.
CORAL_VARS="STRIPE_API_KEY HUBSPOT_ACCESS_TOKEN INTERCOM_ACCESS_TOKEN \
SLACK_TOKEN NOTION_API_KEY PAGERDUTY_API_TOKEN \
DD_SITE DD_API_KEY DD_APPLICATION_KEY \
SENTRY_ORG SENTRY_TOKEN \
POSTHOG_API_BASE POSTHOG_API_KEY"

# Platform secrets consumed by the API + worker.
PLATFORM_VARS="GOOGLE_API_KEY DATABASE_URL STRIPE_WEBHOOK_SECRET \
EVENT_SIGNING_KEY SOURCE_CONFIG_KEY CLERK_SECRET_KEY"

KNOWN_VARS="$CORAL_VARS $PLATFORM_VARS"

secret_name_for() {
    # Maps an env var name to its Secret Manager secret id.
    local var="$1" lower
    lower="$(printf '%s' "$var" | tr '[:upper:]_' '[:lower:]-')"
    case "$var" in
        GOOGLE_API_KEY)
            echo "manthan-${TENANT}-gemini-api-key" ;;
        DATABASE_URL|STRIPE_WEBHOOK_SECRET|EVENT_SIGNING_KEY|SOURCE_CONFIG_KEY|CLERK_SECRET_KEY)
            echo "manthan-${TENANT}-${lower}" ;;
        *)
            echo "coral-${TENANT}-${lower}" ;;
    esac
}

env_value() {
    # Last assignment wins; tolerates optional `export ` and surrounding
    # single/double quotes. Prints nothing if the var is absent.
    local var="$1" line value
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${var}=" "$ENV_FILE" | tail -n 1 || true)"
    [ -n "$line" ] || return 0
    value="${line#*=}"
    # strip one layer of matching quotes
    case "$value" in
        \"*\") value="${value#\"}"; value="${value%\"}" ;;
        \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    printf '%s' "$value"
}

created=0
updated=0
skipped=0

for var in $KNOWN_VARS; do
    value="$(env_value "$var")"
    if [ -z "$value" ]; then
        skipped=$((skipped + 1))
        continue
    fi

    secret="$(secret_name_for "$var")"

    if gcloud secrets describe "$secret" --project "$PROJECT_ID" >/dev/null 2>&1; then
        printf '%s' "$value" | gcloud secrets versions add "$secret" \
            --project "$PROJECT_ID" --data-file=- >/dev/null
        echo "updated  ${var} -> ${secret}"
        updated=$((updated + 1))
    else
        gcloud secrets create "$secret" \
            --project "$PROJECT_ID" \
            --replication-policy=automatic >/dev/null
        printf '%s' "$value" | gcloud secrets versions add "$secret" \
            --project "$PROJECT_ID" --data-file=- >/dev/null
        echo "created  ${var} -> ${secret}"
        created=$((created + 1))
    fi

    # Per-secret IAM: the runtime SA can read THIS secret and nothing else.
    gcloud secrets add-iam-policy-binding "$secret" \
        --project "$PROJECT_ID" \
        --member "serviceAccount:${SERVICE_ACCOUNT}" \
        --role "roles/secretmanager.secretAccessor" >/dev/null
    echo "  iam    secretAccessor -> ${SERVICE_ACCOUNT}"
done

echo
echo "secrets-bootstrap: ${created} created, ${updated} updated, ${skipped} known vars absent from ${ENV_FILE}"
