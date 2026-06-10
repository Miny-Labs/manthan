#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# worker-entrypoint.sh — Cloud Run command override for manthan-worker.
#
# The investigate worker is a PG LISTEN/NOTIFY consumer with no HTTP
# server, but Cloud Run *services* require the container to listen on
# $PORT to pass the startup probe. So this script:
#
#   1. registers Coral sources (coral-bootstrap.sh — never fails the boot),
#   2. starts a tiny stdlib HTTP listener on $PORT from an EMPTY dir
#      (satisfies the probe; serves nothing of value; the service is
#      deployed --no-allow-unauthenticated anyway),
#   3. exec's the real worker: python -m manthan_api.workers.main.
#
# Used by deploy.sh:
#   gcloud run deploy manthan-worker ... --command=/usr/local/bin/worker-entrypoint.sh
# ──────────────────────────────────────────────────────────────────────
set -uo pipefail

/usr/local/bin/coral-bootstrap.sh || \
    echo "[worker-entrypoint] WARN: coral-bootstrap reported a problem (continuing)" >&2

# Health shim for the Cloud Run startup/liveness probe.
HEALTH_DIR="$(mktemp -d)"
( cd "$HEALTH_DIR" && exec python -m http.server "${PORT:-8080}" --bind 0.0.0.0 ) \
    >/dev/null 2>&1 &
echo "[worker-entrypoint] health listener on :${PORT:-8080} (pid $!)"

cd /app/manthan-api
echo "[worker-entrypoint] starting python -m manthan_api.workers.main"
exec python -m manthan_api.workers.main
