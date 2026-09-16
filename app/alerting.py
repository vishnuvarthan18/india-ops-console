"""Section 5 — alert episodes and notifications.

The difference between this and the existing alert views: a view answers "what
is wrong now" and forgets everything the moment it clears. This turns each
condition into an *episode* with an opened_at and a resolved_at, so "what broke
last Tuesday, and when did it clear" is answerable a month later.

Notification policy, which is the part that decides whether alerts stay useful:

  * Fire on TRANSITIONS only — a new episode opening, or an episode closing.
    Never on every poll. A problem that lasts a week produces two messages, not
    a thousand.
  * The open-episode uniqueness index in migration 0021 is what enforces this
    structurally; the notifier does not have to remember what it already sent.
  * A resolution message is sent too. An alert channel that only ever delivers
    bad news trains you to dread it and eventually to mute it.
"""

import json
import logging
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

from app.db import cursor, fetch_all
from app.settings import get_settings

logger = logging.getLogger(__name__)


def current_conditions() -> list[dict]:
    """Everything currently wrong, as (alert_key, kind, severity, subject, detail).

    Deliberately reads the same views the console displays, so a condition can
    never be alertable but invisible, or visible but never alerted.
    """
    out: list[dict] = []

    for r in fetch_all("SELECT job_key, reason, last_status::text AS last_status, "
                       "consecutive_failures, silence FROM v_heartbeat_alert"):
        out.append({
            "alert_key": f"heartbeat:{r['job_key']}",
            "kind": "heartbeat",
            # A job that has failed is worse than one that is merely quiet.
            "severity": "critical" if r["reason"] in ("last_run_failed", "consecutive_failures") else "warn",
            "subject": r["job_key"],
            "detail": f"{r['reason']}; last status {r['last_status'] or 'never'}, "
                      f"{r['consecutive_failures']} consecutive failure(s)",
        })

    for r in fetch_all("SELECT key, engine, age FROM v_stale_source"):
        out.append({
            "alert_key": f"stale_source:{r['key']}",
            "kind": "stale_source",
            "severity": "warn",
            "subject": r["key"],
            "detail": f"{r['engine']}: past its staleness ceiling (age {r['age']})",
        })

    for r in fetch_all(
        """
        SELECT s.key AS source_key, max(r.finished_at) AS latest, count(*) AS n
        FROM harvest_run r JOIN source s ON s.id = r.source_id
        WHERE r.status = 'success' AND r.records_accepted = 0
          AND r.finished_at > now() - interval '7 days'
        GROUP BY s.key
        """
    ):
        out.append({
            "alert_key": f"empty_run:{r['source_key']}",
            "kind": "empty_run",
            # The dangerous failure: a job that reports success while bringing
            # nothing home. Always critical.
            "severity": "critical",
            "subject": r["source_key"],
            "detail": f"{r['n']} successful run(s) accepted 0 records, latest {r['latest']}",
        })

    return out


def sync_episodes() -> dict:
    """Open episodes for new conditions, close those that have cleared.
    Returns the transitions, which are exactly what gets notified."""
    conditions = {c["alert_key"]: c for c in current_conditions()}
    opened, resolved = [], []

    with cursor(commit=True) as cur:
        cur.execute(
            "SELECT id, alert_key, kind, severity, subject, detail "
            "FROM ops_console.alert_event WHERE resolved_at IS NULL"
        )
        open_now = {r["alert_key"]: r for r in cur.fetchall()}

        for key, c in conditions.items():
            if key in open_now:
                continue
            cur.execute(
                """
                INSERT INTO ops_console.alert_event (alert_key, kind, severity, subject, detail)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (alert_key) WHERE resolved_at IS NULL DO NOTHING
                RETURNING id, alert_key, kind, severity, subject, detail
                """,
                (key, c["kind"], c["severity"], c["subject"], c["detail"]),
            )
            row = cur.fetchone()
            if row:
                opened.append(row)

        for key, row in open_now.items():
            if key in conditions:
                continue
            cur.execute(
                "UPDATE ops_console.alert_event SET resolved_at = now() WHERE id = %s "
                "RETURNING id, alert_key, kind, severity, subject, detail, opened_at",
                (row["id"],),
            )
            resolved.append(cur.fetchone())

    return {"opened": opened, "resolved": resolved, "still_open": len(conditions)}


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _post_webhook(url: str, payload: dict) -> tuple[bool, str]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _send_email(subject: str, body: str) -> tuple[bool, str]:
    s = get_settings()
    if not (s.smtp_host and s.alert_email_to and s.alert_email_from):
        return False, "email not configured"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s.alert_email_from
    msg["To"] = s.alert_email_to
    msg.set_content(body)
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=25) as smtp:
            if s.smtp_starttls:
                smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password or "")
            smtp.send_message(msg)
        return True, "sent"
    except Exception as exc:  # noqa: BLE001
        logger.exception("email send failed")
        return False, str(exc)


def notify(transitions: dict) -> dict:
    """Deliver one message per transition batch, and record that it was sent.

    Delivery failure is logged but never raises: a broken webhook must not stop
    the alert *history* from being written, which is the part that still works
    when notifications do not.
    """
    s = get_settings()
    results = []

    for row in transitions.get("opened", []):
        line = f"[{row['severity'].upper()}] {row['kind']}: {row['subject']} — {row['detail']}"
        ok = _deliver(s, f"Alert opened: {row['subject']}", line, row)
        _mark(row["id"], "notified_at", ok)
        results.append(("opened", row["alert_key"], ok))

    for row in transitions.get("resolved", []):
        line = f"[RESOLVED] {row['kind']}: {row['subject']} (open since {row['opened_at']})"
        ok = _deliver(s, f"Alert resolved: {row['subject']}", line, row)
        _mark(row["id"], "resolve_notified_at", ok)
        results.append(("resolved", row["alert_key"], ok))

    return {"delivered": results}


def _deliver(s, subject: str, body: str, row: dict) -> bool:
    sent_any = False
    if s.alert_webhook_url:
        ok, detail = _post_webhook(s.alert_webhook_url, {
            "text": f"{subject}\n{body}",
            "alert_key": row["alert_key"], "kind": row["kind"],
            "severity": row["severity"], "subject": row["subject"],
        })
        sent_any = sent_any or ok
        if not ok:
            logger.warning("webhook delivery failed: %s", detail)
    if s.smtp_host:
        ok, detail = _send_email(subject, body)
        sent_any = sent_any or ok
        if not ok:
            logger.warning("email delivery failed: %s", detail)
    if not s.alert_webhook_url and not s.smtp_host:
        logger.info("no notification channel configured; episode recorded only: %s", subject)
    return sent_any


def _mark(event_id: int, column: str, ok: bool) -> None:
    if not ok:
        return
    # Column name is a literal from this module, never from a request.
    assert column in ("notified_at", "resolve_notified_at")
    with cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE ops_console.alert_event SET {column} = now() WHERE id = %s",
            (event_id,),
        )


def history(limit: int = 100) -> list[dict]:
    return fetch_all(
        """
        SELECT id, alert_key, kind, severity, subject, detail,
               opened_at, resolved_at, notified_at,
               COALESCE(resolved_at, now()) - opened_at AS duration
        FROM ops_console.alert_event
        ORDER BY (resolved_at IS NULL) DESC, opened_at DESC
        LIMIT %s
        """,
        (limit,),
    )
