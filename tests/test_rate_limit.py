"""Throttling repeated failed sign-ins.

Argon2 already makes each guess slow. This makes a burst of guesses pointless,
and — more usefully — makes a brute-force attempt visible in the log rather
than indistinguishable from normal traffic.
"""

import pytest

from tests.conftest import ADMIN_PASSWORD, needs_db


@pytest.fixture(autouse=True)
def clean_slate():
    """Each test starts with no recorded failures, since the counter is
    process-wide and shared between tests."""
    from app import auth
    with auth._lock:
        auth._attempts.clear()
    yield
    with auth._lock:
        auth._attempts.clear()


@needs_db
def test_repeated_failures_lock_the_door(client):
    client.post("/logout")
    for _ in range(5):
        assert "Wrong username" in client.post(
            "/login", data={"username": "admin", "password": "wrong"}).text

    r = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert "Too many failed attempts" in r.text


@needs_db
def test_a_locked_out_client_cannot_get_in_with_the_right_password(client):
    """The lockout must not be bypassable by finally guessing correctly —
    otherwise it only slows an attacker down until the moment it matters."""
    client.post("/logout")
    for _ in range(6):
        client.post("/login", data={"username": "admin", "password": "wrong"})

    r = client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 200
    assert "Too many failed attempts" in r.text
    assert client.get("/", follow_redirects=False).status_code == 302


@needs_db
def test_a_correct_sign_in_clears_the_count(client):
    """One fat-fingered evening must not lock the owner out of their own tool."""
    client.post("/logout")
    for _ in range(3):
        client.post("/login", data={"username": "admin", "password": "wrong"})

    assert client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD},
                       follow_redirects=False).status_code == 302
    client.post("/logout")

    for _ in range(3):
        r = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert "Too many failed" not in r.text
    client.post("/logout")


@needs_db
def test_the_message_never_reveals_which_field_was_wrong(client):
    client.post("/logout")
    wrong_user = client.post("/login", data={"username": "nope", "password": "nope"}).text
    from app import auth
    with auth._lock:
        auth._attempts.clear()
    wrong_pass = client.post("/login", data={"username": "admin", "password": "nope"}).text
    assert "Wrong username or password." in wrong_user
    assert "Wrong username or password." in wrong_pass


def test_the_tracking_table_cannot_grow_without_bound():
    """A flood from many addresses must not be able to exhaust memory."""
    from app import auth
    with auth._lock:
        auth._attempts.clear()
    for i in range(auth.MAX_TRACKED_CLIENTS * 2):
        auth.record_failure(f"10.0.{i // 256}.{i % 256}")
    with auth._lock:
        assert len(auth._attempts) <= auth.MAX_TRACKED_CLIENTS + 1
        auth._attempts.clear()


def test_proxy_headers_are_not_trusted():
    """This service binds to localhost. An X-Forwarded-For here would be
    attacker-controlled, so identifying clients by it would hand any attacker
    an unlimited supply of fresh identities."""
    import ast
    import inspect

    from app import auth

    # Read the function body only. Its docstring mentions X-Forwarded-For to
    # explain why it is ignored, and a naive text search would match that.
    tree = ast.parse(inspect.getsource(auth.client_key).strip())
    fn = tree.body[0]
    body = fn.body[1:] if ast.get_docstring(fn) else fn.body
    code = "\n".join(ast.unparse(node) for node in body)
    assert "forwarded" not in code.lower()
    assert "headers" not in code.lower()
    assert "request.client" in code


def test_lockout_expires():
    """A lockout is a delay, not a ban: the owner must get back in."""
    from app import auth
    with auth._lock:
        auth._attempts.clear()
    key = "test-client"
    for _ in range(auth.MAX_ATTEMPTS):
        auth.record_failure(key)
    assert auth.lockout_remaining(key) > 0

    # move every recorded failure past the lockout window
    with auth._lock:
        auth._attempts[key] = [t - (auth.LOCKOUT_SECONDS + 1)
                               for t in auth._attempts[key]]
    assert auth.lockout_remaining(key) == 0
    with auth._lock:
        auth._attempts.clear()
