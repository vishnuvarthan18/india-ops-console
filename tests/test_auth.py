"""Who can reach what."""

import pytest

from tests.conftest import ADMIN_PASSWORD, needs_db

READ_ENDPOINTS = [
    "/", "/alerts", "/engines", "/engines/protected_areas", "/data",
    "/data/export.csv", "/entity/pa:protected_area:res-1", "/map",
    "/map/data.geojson", "/server", "/decisions", "/keys", "/open-items",
]

WRITE_ENDPOINTS = [
    ("/open-items/1/status", {"status_value": "done"}),
    ("/keys/1/status", {"status_value": "registered"}),
    ("/keys/DATA_GOV_IN_API_KEY/rotate", {"value": "attacker-value"}),
    ("/jobs/pa-harvest.service/run", {}),
]


@needs_db
@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_endpoints_require_a_session(client, path):
    client.post("/logout")
    r = client.get(path, follow_redirects=False)
    assert r.status_code in (302, 401), f"{path} served content to a signed-out visitor"


@needs_db
@pytest.mark.parametrize("path,data", WRITE_ENDPOINTS)
def test_write_endpoints_require_a_session(client, path, data):
    client.post("/logout")
    r = client.post(path, data=data, follow_redirects=False)
    assert r.status_code in (302, 401, 403), f"{path} accepted a signed-out write"


@needs_db
def test_wrong_password_is_rejected(client):
    client.post("/logout")
    r = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert "Wrong username" in r.text


@needs_db
def test_wrong_username_is_rejected(client):
    client.post("/logout")
    r = client.post("/login", data={"username": "root", "password": ADMIN_PASSWORD})
    assert "Wrong username" in r.text


@needs_db
def test_correct_credentials_sign_in(client):
    client.post("/logout")
    r = client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 302
    client.post("/logout")


@needs_db
def test_session_cookie_is_hardened(client):
    client.post("/logout")
    r = client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    cookie = r.headers.get("set-cookie", "") or ""
    for prev in getattr(r, "history", []):
        cookie = cookie or prev.headers.get("set-cookie", "")
    assert "httponly" in cookie.lower(), "session cookie must not be readable by JavaScript"
    assert "samesite=lax" in cookie.lower(), (
        "SameSite=Lax is what blocks a cross-site form POST from reaching this "
        "console while the SSH tunnel is open"
    )
    client.post("/logout")


@needs_db
def test_logout_actually_ends_the_session(client):
    client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    assert client.get("/", follow_redirects=False).status_code == 200
    client.post("/logout")
    assert client.get("/", follow_redirects=False).status_code == 302


@needs_db
def test_path_traversal_in_decisions_is_refused(signed_in):
    """Only the forms that actually reach the server are asserted here.

    An HTTP client normalises `/decisions/..` to `/decisions` before sending,
    so asserting on that would be testing the client, not the console. The
    encoded forms below do arrive intact — and the resolved-path check is
    covered directly in test_decisions_module_rejects_traversal.
    """
    for attempt in ["..%2f..%2fetc", "..%2f..%2f..%2fetc%2fpasswd",
                    "%2e%2e%2f%2e%2e", ".ssh", ".git"]:
        r = signed_in.get(f"/decisions/{attempt}")
        assert r.status_code in (404, 403), f"{attempt} returned {r.status_code}"


def test_decisions_module_rejects_traversal(tmp_path, monkeypatch):
    """The real defence, tested where it lives: a name is rejected before disk
    is touched, and a symlink cannot walk the reader out of the mounted repos
    directory after resolution."""
    from app import decisions
    from app.settings import get_settings

    root = tmp_path / "repos"
    (root / "good").mkdir(parents=True)
    (root / "good" / "DECISIONS.md").write_text("# fine")
    secret = tmp_path / "outside"
    secret.mkdir()
    (secret / "DECISIONS.md").write_text("should never be readable")
    (root / "sneaky").mkdir()
    (root / "sneaky" / "DECISIONS.md").symlink_to(secret / "DECISIONS.md")

    get_settings.cache_clear()
    monkeypatch.setenv("REPOS_DIR", str(root))
    monkeypatch.setenv("POSTGRES_PASSWORD", "x")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "x")
    monkeypatch.setenv("SESSION_SECRET", "x")
    get_settings.cache_clear()
    try:
        assert decisions.read_decisions("good") is not None
        for bad in ["../outside", "..", ".hidden", "good/../../outside", "/etc"]:
            assert decisions.read_decisions(bad) is None, f"{bad} was not rejected"
        doc = decisions.read_decisions("sneaky")
        assert doc is None, "a symlink walked the reader outside the repos directory"
    finally:
        get_settings.cache_clear()


@needs_db
def test_unknown_records_404(signed_in):
    assert signed_in.get("/entity/no:such:thing").status_code == 404
    assert signed_in.get("/engines/no-such-engine").status_code == 404


@needs_db
def test_invalid_status_values_are_refused(signed_in):
    assert signed_in.post("/open-items/1/status",
                          data={"status_value": "'; drop table entity; --"}).status_code == 422


@needs_db
def test_non_allowlisted_unit_is_refused_through_the_app(signed_in):
    """Even with a session, the console cannot reach a unit the control
    service's allowlist does not contain."""
    assert signed_in.get("/jobs/docker.service").status_code in (403, 503)
    assert signed_in.post("/jobs/ssh.service/run",
                          follow_redirects=False).status_code in (403, 503)
