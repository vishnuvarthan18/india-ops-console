"""Migrations must be safely re-runnable.

A migration that fails halfway on a second run is one nobody dares re-run,
which means nobody re-runs any of them, which is how a deploy goes wrong. This
caught a missing DROP TRIGGER IF EXISTS during development.
"""

import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import needs_db

# Migrations alter tables and create roles, so they run as the superuser — not
# as the deliberately-powerless role the console itself uses.
ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL", "")

MIGRATIONS = sorted((Path(__file__).resolve().parents[1] / "db" / "migrations").glob("*.sql"))


def _psql(path: Path):
    return subprocess.run(
        ["psql", ADMIN_URL, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(path)],
        capture_output=True, text=True,
    )


@pytest.mark.skipif(not MIGRATIONS, reason="no migrations found")
@pytest.mark.skipif(not ADMIN_URL, reason="set TEST_ADMIN_DATABASE_URL")
@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_migration_is_idempotent(path):
    if subprocess.run(["which", "psql"], capture_output=True).returncode != 0:
        pytest.skip("psql not installed")
    # The suite's own database already has these applied once; running each
    # again twice proves re-running is safe.
    for attempt in (1, 2):
        result = _psql(path)
        assert result.returncode == 0, (
            f"{path.name} failed on re-run {attempt}:\n{result.stderr[-2000:]}"
        )


@needs_db
def test_ops_console_role_cannot_write_engine_data(client):
    """The structural control: even a fully compromised console cannot alter a
    single collected fact."""
    import psycopg

    from app.settings import get_settings

    s = get_settings()
    if s.postgres_user != "ops_console":
        pytest.skip("not connected as the restricted role")

    with psycopg.connect(s.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM entity")          # reading is fine
        for stmt in [
            "DELETE FROM entity_fact",
            "UPDATE entity SET name = 'tampered'",
            "INSERT INTO engine (key, name) VALUES ('x','x')",
            "DROP TABLE entity_fact",
        ]:
            conn.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(stmt)
        conn.rollback()


@needs_db
def test_ops_console_role_can_write_its_own_schema(client):
    """It must still be able to tick an open item off."""
    from app.db import cursor

    with cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO ops_console.open_item (title, category) "
            "VALUES ('test item', 'dev') RETURNING id"
        )
        new_id = cur.fetchone()["id"]
        cur.execute("DELETE FROM ops_console.open_item WHERE id = %s", (new_id,))
