"""Shared fixtures.

Two kinds of test live here. Ones that need a database are skipped with a clear
message when TEST_DATABASE_URL is unset, so `pytest` still does something useful
on a laptop with no Docker — notably the whole control-service suite, which is
the most security-sensitive code in the repo and needs no database at all.
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ADMIN_PASSWORD = "test-password-not-a-real-one"
DB_URL = os.environ.get("TEST_DATABASE_URL", "")

needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="set TEST_DATABASE_URL, or run ./scripts/run-tests.sh which provides one",
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _dsn_parts(url: str) -> dict:
    """Split a postgres URL into the environment the app's settings expect."""
    from urllib.parse import unquote, urlparse

    u = urlparse(url)
    return {
        "POSTGRES_HOST": u.hostname or "127.0.0.1",
        "POSTGRES_PORT": str(u.port or 5432),
        "POSTGRES_USER": unquote(u.username or "postgres"),
        "POSTGRES_PASSWORD": unquote(u.password or ""),
        "POSTGRES_DB": (u.path or "/postgres").lstrip("/"),
    }


@pytest.fixture(scope="session")
def app_env(tmp_path_factory):
    """Environment for the console under test. Built once per session."""
    from argon2 import PasswordHasher

    metrics_dir = tmp_path_factory.mktemp("metrics")
    (metrics_dir / "metrics.json").write_text(
        '{"collected_at":"2026-01-01T00:00:00Z","hostname":"test-host",'
        '"uptime":"up 1 day","cpu_count":2,"load":{"1m":0.1,"5m":0.1,"15m":0.1},'
        '"memory":{"total_mb":4000,"used_mb":1000,"use_percent":25},'
        '"disk":{"size":"40G","used":"8G","use_percent":20,'
        '"breakdown":[{"name":"Postgres volume","size":"280M"}]},'
        '"containers":[{"name":"core-postgres","state":"running","status":"Up (healthy)","mem":null}],'
        '"backup":{"latest":"dump.sql.gz","age_hours":12,"count":7,"total_size":"400M"}}'
    )

    repos = tmp_path_factory.mktemp("repos")
    (repos / "india-data-core").mkdir()
    (repos / "india-data-core" / "DECISIONS.md").write_text(
        "# Decisions\n\n## D-70 — heartbeat window\nPlaceholder.\n\n"
        "## D-71 — exit codes\nPartial exits 0.\n"
    )

    env = {
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD_HASH": PasswordHasher().hash(ADMIN_PASSWORD),
        "SESSION_SECRET": "test-session-secret-not-a-real-one-0123456789",
        "METRICS_FILE": str(metrics_dir / "metrics.json"),
        "REPOS_DIR": str(repos),
        "LOG_LEVEL": "ERROR",
        "CONTROL_URL": "http://127.0.0.1:1",   # deliberately dead unless a test overrides
        "CONTROL_TOKEN": "",
    }
    if DB_URL:
        env.update(_dsn_parts(DB_URL))
    return env


@pytest.fixture(scope="session")
def client(app_env):
    """A TestClient with the app freshly imported under the test environment."""
    if not DB_URL:
        pytest.skip("no TEST_DATABASE_URL")
    for k, v in app_env.items():
        os.environ[k] = v
    # settings are cached, so clear anything imported by an earlier test module
    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]

    from fastapi.testclient import TestClient

    from app.main import app

    # raise_server_exceptions=False so the app's own error handler answers,
    # rather than TestClient re-raising and hiding the page a real browser
    # would get.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def admin_db():
    """A connection as the *superuser*, for fixtures that must write to tables
    the console itself is correctly forbidden from touching.

    That the app's own role cannot do this is the point — see
    test_migrations.py::test_ops_console_role_cannot_write_engine_data.
    """
    import psycopg

    url = os.environ.get("TEST_ADMIN_DATABASE_URL", "")
    if not url:
        pytest.skip("set TEST_ADMIN_DATABASE_URL for fixtures needing write access")
    with psycopg.connect(url, autocommit=True) as conn:
        yield conn


@pytest.fixture
def signed_in(client):
    client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    yield client
    client.post("/logout")


# ---------------------------------------------------------------------------
# The control service, started for real against temporary allowlists.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def control_service(tmp_path_factory):
    """Runs the actual control_service.py with a fake systemctl on PATH.

    Fake, because the tests must be able to run anywhere — but the code under
    test is the real thing, including its allowlist handling, which is the part
    that matters.
    """
    import secrets

    d = tmp_path_factory.mktemp("control")
    (d / "units.allow").write_text(
        "# comment line, must be ignored\npa-harvest.service\nculture-fra-jk.service\n"
    )
    (d / "keyvars.allow").write_text("DATA_GOV_IN_API_KEY\nWDPA_API_KEY\n")
    secrets_file = d / "secrets.env"
    secrets_file.write_text(
        "POSTGRES_PASSWORD=must-not-change\nDATA_GOV_IN_API_KEY=old-value\n"
    )

    bindir = d / "bin"
    bindir.mkdir()
    for name in ("systemctl", "journalctl"):
        f = bindir / name
        f.write_text(f'#!/bin/sh\necho "FAKE {name} $*"\nexit 0\n')
        f.chmod(0o755)

    token = secrets.token_urlsafe(48)
    port = free_port()
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "CONTROL_TOKEN": token,
        "CONTROL_PORT": str(port),
        "CONTROL_ALLOWLIST": str(d / "units.allow"),
        "CONTROL_KEYVARS": str(d / "keyvars.allow"),
        "CONTROL_SECRETS_FILE": str(secrets_file),
    }
    script = Path(__file__).resolve().parents[1] / "control" / "control_service.py"
    log = open(d / "service.log", "w")
    proc = subprocess.Popen([sys.executable, str(script)], env=env,
                            stdout=log, stderr=subprocess.STDOUT)

    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("control service did not start")

    yield {"base": base, "token": token, "dir": d,
           "secrets_file": secrets_file, "log": d / "service.log"}

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()
