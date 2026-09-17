"""The staleness logic — the D-70 fix, locked in.

Before this fix every job was allowed 36 hours of silence. Eight of the sixteen
live jobs are monthly and four are weekly, so two thirds of the fleet was
permanently flagged as broken between perfectly healthy runs.

These tests insert a fleet with known shapes and assert on exactly which of them
alert. If someone later reverts to a flat window, the monthly-job test fails.
"""

import pytest

from tests.conftest import needs_db


@pytest.fixture
def fleet(client, admin_db):
    """A fleet covering every case the window logic has to distinguish.

    Written through the superuser connection, because the console's own role
    is correctly denied write access to heartbeat.
    """

    rows = [
        # (job_key, cadence, last_ping interval or None, status, created interval)
        ("t-daily-ok",        "daily",   "6 hours",  "success", "90 days"),
        ("t-daily-late",      "daily",   "5 days",   "success", "90 days"),
        ("t-weekly-ok",       "weekly",  "4 days",   "success", "90 days"),
        ("t-monthly-ok",      "monthly", "20 days",  "success", "90 days"),
        ("t-monthly-edge",    "monthly", "29 days",  "success", "90 days"),
        ("t-monthly-late",    "monthly", "80 days",  "success", "90 days"),
        ("t-failed",          "daily",   "1 hour",   "failed",  "90 days"),
        ("t-new-monthly",     "monthly", None,       None,      "2 days"),
        ("t-forgotten",       "monthly", None,       None,      "200 days"),
    ]
    with admin_db.cursor() as cur:
        cur.execute("DELETE FROM heartbeat WHERE job_key LIKE 't-%'")
        for key, cadence, ping, status, created in rows:
            # Every placeholder is cast explicitly: a bare NULL leaves Postgres
            # unable to infer the type and the insert fails with
            # "could not determine data type of parameter".
            cur.execute(
                """
                INSERT INTO heartbeat (job_key, cadence, last_ping_at, last_status,
                                       consecutive_failures, created_at)
                VALUES (%s,
                        %s::schedule_tier,
                        CASE WHEN %s::text IS NULL THEN NULL
                             ELSE now() - %s::text::interval END,
                        %s::run_status,
                        %s,
                        now() - %s::text::interval)
                """,
                (key, cadence, ping, ping, status,
                 1 if status == "failed" else 0, created),
            )
    yield
    with admin_db.cursor() as cur:
        cur.execute("DELETE FROM heartbeat WHERE job_key LIKE 't-%'")


def _alerting(keys_only=True):
    from app.db import fetch_all
    rows = fetch_all("SELECT job_key, reason FROM v_heartbeat_alert WHERE job_key LIKE 't-%%'")
    return {r["job_key"] for r in rows} if keys_only else rows


@needs_db
def test_healthy_monthly_job_is_not_stale(client, fleet):
    """The whole point of D-70. Twenty days of silence from a monthly job is
    normal, not a fault."""
    assert "t-monthly-ok" not in _alerting()


@needs_db
def test_monthly_job_just_before_its_next_run_is_not_stale(client, fleet):
    assert "t-monthly-edge" not in _alerting()


@needs_db
def test_genuinely_overdue_monthly_job_does_alert(client, fleet):
    """Tolerance must not become permissiveness."""
    assert "t-monthly-late" in _alerting()


@needs_db
def test_healthy_daily_and_weekly_jobs_are_not_stale(client, fleet):
    alerting = _alerting()
    assert "t-daily-ok" not in alerting
    assert "t-weekly-ok" not in alerting


@needs_db
def test_silent_daily_job_alerts(client, fleet):
    assert "t-daily-late" in _alerting()


@needs_db
def test_failed_job_alerts_regardless_of_timing(client, fleet):
    assert "t-failed" in _alerting()


@needs_db
def test_newly_registered_job_is_not_called_broken(client, fleet):
    """A job registered two days ago that has never run is waiting, not
    broken. Getting this wrong is what made six healthy jobs look alarming
    during the September observation pause."""
    assert "t-new-monthly" not in _alerting()


@needs_db
def test_long_registered_job_that_never_ran_does_alert(client, fleet):
    assert "t-forgotten" in _alerting()


@needs_db
def test_a_flat_window_would_have_been_far_noisier(client, fleet):
    """Documents the size of the fix: under the old flat 36-hour rule, most of
    this fleet was permanently red."""
    from app.db import fetch_all
    flat = {r["job_key"] for r in fetch_all(
        "SELECT job_key FROM heartbeat WHERE job_key LIKE 't-%%' "
        "AND (last_ping_at IS NULL OR now() - last_ping_at > interval '36 hours')"
    )}
    assert len(_alerting()) < len(flat)


@needs_db
def test_every_alert_says_why(client, fleet):
    """An alert without a reason makes the reader do the diagnosis twice."""
    for row in _alerting(keys_only=False):
        assert row["reason"], f"{row['job_key']} alerts with no reason given"


@needs_db
def test_attention_list_ranks_failures_first(client, fleet):
    from app import queries
    rows = queries.attention(limit=20)
    severities = [r["severity"] for r in rows]
    if "critical" in severities and "warn" in severities:
        assert severities.index("critical") < severities.index("warn")


@needs_db
def test_attention_rows_all_have_somewhere_to_go(client, fleet):
    """No dead ends: every row offers either a job to inspect or an engine to open."""
    from app import queries
    for r in queries.attention(limit=20):
        assert r.get("unit") or r.get("link"), f"{r['subject']} is a dead end"
