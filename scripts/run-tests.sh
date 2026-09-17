#!/usr/bin/env bash
# Run the whole suite against a disposable database.
#
# Starts a PostGIS container, applies the real schema, seeds the fixture, runs
# pytest, then removes the container whether the tests passed or not.
#
# Needs Docker. Everything else is installed into a throwaway virtualenv.
#
#   ./scripts/run-tests.sh              # everything
#   ./scripts/run-tests.sh -k control   # just the control-service tests
#
# The core schema lives in a sibling repo. Point CORE_REPO elsewhere if yours
# is somewhere else.

set -euo pipefail
cd "$(dirname "$0")/.."

CORE_REPO="${CORE_REPO:-../india-data-core}"
PG_IMAGE="${PG_IMAGE:-postgis/postgis:17-3.5}"
CONTAINER="ops-console-tests-$$"
PGPORT="${PGPORT:-55432}"
PGPASS="tests-only-throwaway"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if ! command -v docker >/dev/null 2>&1; then
  echo "This script needs Docker. Alternatively point TEST_DATABASE_URL at a" >&2
  echo "database you already have and run: pytest -v" >&2
  exit 2
fi

if [ ! -d "$CORE_REPO/db/migrations" ]; then
  echo "Cannot find the core schema at $CORE_REPO/db/migrations." >&2
  echo "Set CORE_REPO to where india-data-core is checked out." >&2
  exit 2
fi

echo "==> starting a disposable $PG_IMAGE on port $PGPORT"
docker run -d --name "$CONTAINER" \
  -e POSTGRES_PASSWORD="$PGPASS" -e POSTGRES_DB=india_data \
  -p "127.0.0.1:$PGPORT:5432" "$PG_IMAGE" >/dev/null

SUPER="postgresql://postgres:$PGPASS@127.0.0.1:$PGPORT/india_data"

echo -n "==> waiting for it to accept connections"
for _ in $(seq 1 60); do
  if docker exec "$CONTAINER" pg_isready -U postgres -d india_data >/dev/null 2>&1; then
    echo " ok"; break
  fi
  echo -n "."; sleep 1
done

run_sql() {  # run a .sql file inside the container, failing loudly
  docker exec -i "$CONTAINER" psql -U postgres -d india_data -v ON_ERROR_STOP=1 -q < "$1"
}

echo "==> applying the core schema from $CORE_REPO"
for f in "$CORE_REPO"/db/migrations/*.sql; do
  run_sql "$f"
done

echo "==> applying this repo's migrations"
for f in db/migrations/*.sql; do
  run_sql "$f"
done

echo "==> setting the ops_console role's password"
docker exec "$CONTAINER" psql -U postgres -d india_data -q \
  -c "ALTER ROLE ops_console PASSWORD '$PGPASS';" >/dev/null

echo "==> seeding the fixture"
run_sql tests/fixtures/seed.sql

echo "==> installing test dependencies"
VENV="${VENV:-.venv-tests}"
python3 -m venv "$VENV" >/dev/null 2>&1 || true
# shellcheck disable=SC1091
. "$VENV/bin/activate"
pip install -q -r requirements-dev.txt

# Two URLs on purpose. The suite runs the console as its own restricted role,
# which is exactly what proves the role cannot write engine data — so fixtures
# that need to seed a heartbeat use the superuser one instead.
export TEST_DATABASE_URL="postgresql://ops_console:$PGPASS@127.0.0.1:$PGPORT/india_data"
export TEST_ADMIN_DATABASE_URL="$SUPER"
echo "==> running pytest"
echo
set +e
python -m pytest -v --tb=short "$@"
STATUS=$?
set -e

echo
if [ $STATUS -eq 0 ]; then
  echo "==> all tests passed"
else
  echo "==> FAILURES — see above" >&2
fi
exit $STATUS
