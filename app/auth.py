"""Single-admin login.

The plan calls for "a real password + session, not just IP allowlist", because
this app is the operating interface for the whole platform. Argon2id is used
rather than a bare SHA — an admin password is a human-chosen secret and needs a
slow hash, unlike the engines' API keys, which are high-entropy random strings
where SHA-256 is appropriate.

Only the hash is ever stored. Generate one with:
    docker compose run --rm ops-console python -m app.auth hash
"""

import getpass
import logging
import secrets
import sys
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, status

from app.settings import get_settings

logger = logging.getLogger(__name__)
_hasher = PasswordHasher()

SESSION_KEY = "admin"
EPOCH_KEY = "epoch"

# ---------------------------------------------------------------------------
# Throttling repeated failures.
#
# The console is reached over an SSH tunnel, so an attacker has to be on the
# machine already — but "you had to get in first" is a reason to make this cheap,
# not a reason to skip it. Argon2 already makes each guess slow; this makes a
# burst of them useless.
#
# Deliberately in memory rather than in the database: the console must still be
# able to say "wrong password" when Postgres is down, and a restart clearing the
# counter is an acceptable trade for a single-admin tool behind a tunnel.
#
# Counted per client address, with a hard ceiling on how many addresses are
# tracked, so a spoofed-address flood cannot grow this dictionary without bound.
# ---------------------------------------------------------------------------

MAX_ATTEMPTS = 5          # failures before the door closes
LOCKOUT_SECONDS = 300     # how long it stays closed
WINDOW_SECONDS = 900      # failures older than this no longer count
MAX_TRACKED_CLIENTS = 1024

_attempts: dict[str, list[float]] = {}
_lock = threading.Lock()


def _prune(now: float) -> None:
    """Drop expired entries, and if the table is still too big, drop the
    least recently active clients. Called with the lock held."""
    for key in [k for k, v in _attempts.items()
                if not v or now - v[-1] > max(WINDOW_SECONDS, LOCKOUT_SECONDS)]:
        del _attempts[key]
    if len(_attempts) > MAX_TRACKED_CLIENTS:
        for key in sorted(_attempts, key=lambda k: _attempts[k][-1])[:len(_attempts) // 4]:
            del _attempts[key]


def client_key(request) -> str:
    """Identify the caller. No proxy headers are trusted: this service binds to
    127.0.0.1 and is reached through an SSH tunnel, so an X-Forwarded-For here
    would be attacker-controlled and worse than useless."""
    return request.client.host if request and request.client else "unknown"


def lockout_remaining(key: str) -> int:
    """Seconds until this client may try again; 0 when it may try now."""
    now = time.monotonic()
    with _lock:
        _prune(now)
        recent = [t for t in _attempts.get(key, []) if now - t < WINDOW_SECONDS]
        _attempts[key] = recent
        if len(recent) < MAX_ATTEMPTS:
            return 0
        elapsed = now - recent[-1]
        return max(0, int(LOCKOUT_SECONDS - elapsed))


def record_failure(key: str) -> None:
    now = time.monotonic()
    with _lock:
        _prune(now)
        _attempts.setdefault(key, []).append(now)


def clear_failures(key: str) -> None:
    """A correct sign-in wipes the slate, so one fat-fingered evening does not
    lock the owner out of their own console."""
    with _lock:
        _attempts.pop(key, None)


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, stored_hash: str) -> bool:
    try:
        _hasher.verify(stored_hash, plaintext)
        return True
    except (VerifyMismatchError, VerificationError):
        return False
    except Exception:  # noqa: BLE001 — a malformed hash must not 500 the login page
        logger.exception("password hash could not be verified; check ADMIN_PASSWORD_HASH")
        return False


def check_login(username: str, password: str) -> bool:
    s = get_settings()
    # Compare the username in constant time too, so the response time does not
    # reveal whether the username was right.
    user_ok = secrets.compare_digest(username or "", s.admin_username)
    pass_ok = verify_password(password or "", s.admin_password_hash)
    return user_ok and pass_ok


def session_is_current(request: Request) -> bool:
    """A session is valid only if it was issued under the current epoch.

    Without this, the only way to revoke a session was to change
    SESSION_SECRET — a bigger hammer, and one that is easy to put off.
    """
    if not request.session.get(SESSION_KEY):
        return False
    return request.session.get(EPOCH_KEY) == get_settings().session_epoch


def require_admin(request: Request) -> None:
    """Dependency for every page except /login and /healthz."""
    if not session_is_current(request):
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="login required",
        )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "hash":
        pw = getpass.getpass("New admin password: ")
        again = getpass.getpass("Confirm: ")
        if pw != again:
            print("Passwords do not match.", file=sys.stderr)
            sys.exit(1)
        if len(pw) < 12:
            print("Use at least 12 characters.", file=sys.stderr)
            sys.exit(1)
        print("\nPut this in .env as ADMIN_PASSWORD_HASH:\n")
        print(hash_password(pw))
    else:
        print("usage: python -m app.auth hash", file=sys.stderr)
        sys.exit(2)
