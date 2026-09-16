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

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, status

from app.settings import get_settings

logger = logging.getLogger(__name__)
_hasher = PasswordHasher()

SESSION_KEY = "admin"


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


def require_admin(request: Request) -> None:
    """Dependency for every page except /login and /healthz."""
    if not request.session.get(SESSION_KEY):
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
