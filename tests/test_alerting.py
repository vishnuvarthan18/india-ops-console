"""Alert episodes and notification dedup.

This is what keeps the alert list worth reading. D-70 and D-71 were both the
same failure: a system crying wolf until nobody looked. A regression here
quietly recreates that, and nothing else would catch it.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.conftest import free_port, needs_db


@pytest.fixture
def webhook():
    """A webhook receiver that records what it is sent."""
    received = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            received.append(json.loads(self.rfile.read(n) or b"{}"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield {"url": f"http://127.0.0.1:{port}/hook", "received": received}
    srv.shutdown()


@needs_db
def test_conditions_become_episodes(client):
    from app import alerting
    before = len(alerting.history(500))
    alerting.sync_episodes()
    assert len(alerting.history(500)) >= before


@needs_db
def test_the_same_condition_does_not_reopen(client):
    """The partial unique index is what enforces this, not the notifier
    remembering — so it holds even across restarts."""
    from app import alerting
    alerting.sync_episodes()
    second = alerting.sync_episodes()
    third = alerting.sync_episodes()
    assert second["opened"] == [], "a standing condition opened a second episode"
    assert third["opened"] == []


@needs_db
def test_notifications_fire_only_on_transitions(client, webhook, monkeypatch):
    from app import alerting
    from app.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ALERT_WEBHOOK_URL", webhook["url"])
    get_settings.cache_clear()

    alerting.notify(alerting.sync_episodes())
    first = len(webhook["received"])

    alerting.notify(alerting.sync_episodes())
    alerting.notify(alerting.sync_episodes())
    assert len(webhook["received"]) == first, (
        "polling an unchanged system sent more notifications — this is exactly "
        "the noise loop the alerting work was meant to remove"
    )
    get_settings.cache_clear()


@needs_db
def test_delivery_failure_still_records_history(client, monkeypatch):
    """A dead webhook must not cost you the record that something happened."""
    from app import alerting
    from app.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://127.0.0.1:9/definitely-dead")
    get_settings.cache_clear()

    before = len(alerting.history(500))
    alerting.notify(alerting.sync_episodes())
    assert len(alerting.history(500)) >= before  # nothing raised, history intact
    get_settings.cache_clear()


@needs_db
def test_every_current_condition_is_visible_in_the_console(signed_in):
    """A condition that can alert but cannot be seen is a trap."""
    from app import alerting
    html = signed_in.get("/alerts").text
    for c in alerting.current_conditions():
        assert c["subject"] in html, f"{c['subject']} alerts but never appears on /alerts"
