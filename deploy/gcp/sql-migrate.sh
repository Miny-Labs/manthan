#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# sql-migrate.sh — apply manthan-api/schema/*.sql in filename order.
#
# Usage:
#   DATABASE_URL=postgresql://user:pass@host:5432/manthan ./sql-migrate.sh
#   # or
#   ./sql-migrate.sh "postgresql://user:pass@host:5432/manthan"
#
# Against Cloud SQL, run the Cloud SQL Auth Proxy in another terminal:
#   cloud-sql-proxy --port 5432 PROJECT_ID:REGION:INSTANCE
# then point DATABASE_URL at localhost:
#   DATABASE_URL=postgresql://manthan:PASSWORD@127.0.0.1:5432/manthan ./sql-migrate.sh
#
# Migrations are plain DDL applied with ON_ERROR_STOP; they are written
# to be applied once, in order (001..005). There is no schema_migrations
# ledger yet — re-running against an already-migrated database will
# fail on the first duplicate object, which is the safe default.
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

DATABASE_URL="${1:-${DATABASE_URL:-}}"
if [ -z "$DATABASE_URL" ]; then
    echo "ERROR: set DATABASE_URL or pass it as the first argument" >&2
    exit 64
fi

command -v psql >/dev/null 2>&1 || {
    echo "ERROR: psql not found on PATH (brew install libpq / apt install postgresql-client)" >&2
    exit 69
}

# schema/ lives in manthan-api, two levels up from deploy/gcp/.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEMA_DIR="${SCHEMA_DIR:-${SCRIPT_DIR}/../../manthan-api/schema}"

if [ ! -d "$SCHEMA_DIR" ]; then
    echo "ERROR: schema dir not found: $SCHEMA_DIR" >&2
    exit 66
fi

shopt -s nullglob
files=("$SCHEMA_DIR"/*.sql)
shopt -u nullglob

if [ ${#files[@]} -eq 0 ]; then
    echo "ERROR: no .sql files in $SCHEMA_DIR" >&2
    exit 66
fi

# Glob expansion is already lexicographic, which matches the 00N_ prefix
# ordering (001_initial.sql .. 005_auth_signups.sql).
for f in "${files[@]}"; do
    echo "==> applying $(basename "$f")"
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$f"
done

echo "sql-migrate: applied ${#files[@]} migration(s)"
