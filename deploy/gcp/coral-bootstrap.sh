#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# coral-bootstrap.sh — register Coral data sources at container start.
#
# The investigator agent service spawns `coral mcp-stdio` ITSELF per
# investigation (agent coral_session), so this script does NOT start a
# server. It only runs `coral source add <source>` for every source
# whose required credentials are present in the environment (Cloud Run
# injects them from Secret Manager via --set-secrets), warns + skips
# any source with missing vars, and always exits 0 so a partial source
# set never blocks the worker from booting.
#
# Env:
#   SOURCES       space- or comma-separated source list
#                 (default: every source in the map below)
#   CORAL_BINARY  path to the coral binary (default: coral on PATH)
#
# Required env vars per source (keys-only auth):
#   stripe     STRIPE_API_KEY
#   hubspot    HUBSPOT_ACCESS_TOKEN
#   intercom   INTERCOM_ACCESS_TOKEN
#   slack      SLACK_TOKEN
#   notion     NOTION_API_KEY
#   pagerduty  PAGERDUTY_API_TOKEN
#   datadog    DD_SITE + DD_API_KEY + DD_APPLICATION_KEY
#   sentry     SENTRY_ORG + SENTRY_TOKEN
#   posthog    POSTHOG_API_BASE + POSTHOG_API_KEY
# ──────────────────────────────────────────────────────────────────────
set -u

required_vars() {
    case "$1" in
        stripe)     echo "STRIPE_API_KEY" ;;
        hubspot)    echo "HUBSPOT_ACCESS_TOKEN" ;;
        intercom)   echo "INTERCOM_ACCESS_TOKEN" ;;
        slack)      echo "SLACK_TOKEN" ;;
        notion)     echo "NOTION_API_KEY" ;;
        pagerduty)  echo "PAGERDUTY_API_TOKEN" ;;
        datadog)    echo "DD_SITE DD_API_KEY DD_APPLICATION_KEY" ;;
        sentry)     echo "SENTRY_ORG SENTRY_TOKEN" ;;
        posthog)    echo "POSTHOG_API_BASE POSTHOG_API_KEY" ;;
        *)          echo "" ;;
    esac
}

SOURCES="${SOURCES:-stripe hubspot intercom slack notion pagerduty datadog sentry posthog}"
SOURCES="${SOURCES//,/ }"
CORAL_BIN="${CORAL_BINARY:-coral}"

added=0
skipped=0

for src in $SOURCES; do
    vars="$(required_vars "$src")"
    if [ -z "$vars" ]; then
        echo "[coral-bootstrap] WARN: unknown source '${src}' — skipping" >&2
        skipped=$((skipped + 1))
        continue
    fi

    missing=""
    for v in $vars; do
        if [ -z "${!v:-}" ]; then
            missing="${missing} ${v}"
        fi
    done
    if [ -n "$missing" ]; then
        echo "[coral-bootstrap] WARN: skipping '${src}' — missing:${missing}" >&2
        skipped=$((skipped + 1))
        continue
    fi

    # </dev/null guarantees non-interactive behaviour: if the CLI tries
    # to prompt, it gets EOF instead of hanging the container.
    if "$CORAL_BIN" source add "$src" </dev/null; then
        echo "[coral-bootstrap] added source '${src}'"
        added=$((added + 1))
    else
        echo "[coral-bootstrap] WARN: 'coral source add ${src}' failed (continuing)" >&2
        skipped=$((skipped + 1))
    fi
done

echo "[coral-bootstrap] done: ${added} added, ${skipped} skipped"
exit 0
