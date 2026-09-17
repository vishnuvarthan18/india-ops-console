"""Error pages, and revoking sessions without changing the signing secret."""

import pytest

from tests.conftest import ADMIN_PASSWORD, needs_db


# --- error pages -----------------------------------------------------------

@needs_db
def test_a_missing_page_is_a_page_not_a_json_blob(signed_in):
    r = signed_in.get("/entity/no:such:record")
    assert r.status_code == 404
    assert "<html" in r.text
    assert "Go back" in r.text


@needs_db
def test_a_refused_action_explains_itself(signed_in):
    r = signed_in.get("/jobs/docker.service")
    assert r.status_code in (403, 503)
    if r.status_code == 403:
        assert "allowlist" in r.text.lower()


@needs_db
def test_json_endpoints_still_get_json_errors(signed_in):
    """A data endpoint must not answer a fetch() with an HTML page."""
    r = signed_in.get("/map/data.geojson?engine=" + "x" * 300)
    assert r.status_code in (200, 422, 500)
    if r.status_code != 200:
        assert r.headers["content-type"].startswith("application/json")


@needs_db
def test_an_unexpected_failure_shows_a_page_and_leaks_nothing(signed_in, monkeypatch):
    """An error message can carry a connection string or a query fragment, so
    the detail goes to the log and never to the browser."""
    from app import queries

    def boom():
        raise RuntimeError("connection to postgres://user:SECRET@host failed")

    monkeypatch.setattr(queries, "totals", boom)
    r = signed_in.get("/")
    assert r.status_code == 500
    assert "SECRET" not in r.text
    assert "postgres://" not in r.text
    assert "Traceback" not in r.text
    assert "unexpected error" in r.text.lower()


# --- session revocation ----------------------------------------------------

@needs_db
def test_bumping_the_epoch_signs_everyone_out(client, monkeypatch):
    """Before this existed, revoking a session meant changing SESSION_SECRET.
    That works, but it is a bigger hammer — and one that is easy to put off."""
    from app.settings import get_settings

    client.post("/logout")
    client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    assert client.get("/", follow_redirects=False).status_code == 200

    get_settings.cache_clear()
    monkeypatch.setenv("SESSION_EPOCH", "2")
    get_settings.cache_clear()

    assert client.get("/", follow_redirects=False).status_code == 302

    get_settings.cache_clear()
    monkeypatch.delenv("SESSION_EPOCH", raising=False)
    get_settings.cache_clear()


@needs_db
def test_a_session_from_before_an_epoch_bump_cannot_write(client, monkeypatch):
    """Revocation has to cover actions, not just pages."""
    from app.settings import get_settings

    client.post("/logout")
    client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})

    get_settings.cache_clear()
    monkeypatch.setenv("SESSION_EPOCH", "3")
    get_settings.cache_clear()

    r = client.post("/open-items/1/status", data={"status_value": "done"},
                    follow_redirects=False)
    assert r.status_code in (302, 401)

    get_settings.cache_clear()
    monkeypatch.delenv("SESSION_EPOCH", raising=False)
    get_settings.cache_clear()


@needs_db
def test_signing_in_again_after_a_bump_works(client, monkeypatch):
    """A revocation is not a lockout."""
    from app.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("SESSION_EPOCH", "4")
    get_settings.cache_clear()

    client.post("/logout")
    assert client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD},
                       follow_redirects=False).status_code == 302
    assert client.get("/", follow_redirects=False).status_code == 200
    client.post("/logout")

    get_settings.cache_clear()
    monkeypatch.delenv("SESSION_EPOCH", raising=False)
    get_settings.cache_clear()


@needs_db
def test_the_chrome_never_disagrees_with_the_server(client):
    """If a page renders the signed-in shell to someone the server would turn
    away, the interface is lying about their access."""
    client.post("/logout")
    html = client.get("/login").text
    assert "nav-item" not in html
