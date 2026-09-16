"""Section 7 — the API key registry.

No secret value is ever stored here or read back. The registry records that a
key exists, which variable carries it, who is blocked without it, and when it
was last rotated. The value itself lives only in the secrets file on the host,
and only the control service can write it.

That means the console can tell you "IUCN v4 is still pending and it is what is
holding up the Living Species work" without being able to tell you, or anyone
who compromises it, what any key actually is.
"""

import logging

from app.db import cursor, fetch_all, fetch_one

logger = logging.getLogger(__name__)

VALID_STATUS = ("pending", "registered", "not_needed")


def registry() -> list[dict]:
    return fetch_all(
        """
        SELECT k.id, k.env_var, k.label, k.provider, k.engine_key, k.source_keys,
               k.status, k.register_url, k.notes, k.rotation_days,
               k.last_rotated_at, k.service_unit, k.updated_at,
               e.name AS engine_name,
               CASE
                 WHEN k.rotation_days IS NULL OR k.last_rotated_at IS NULL THEN NULL
                 ELSE (k.last_rotated_at + make_interval(days => k.rotation_days)) < now()
               END AS rotation_overdue,
               COALESCE(blocked.n, 0) AS blocked_sources
        FROM ops_console.api_key_registry k
        LEFT JOIN engine e ON e.key = k.engine_key
        LEFT JOIN (
          SELECT api_key_env_var, count(*) AS n
          FROM source
          WHERE requires_api_key AND api_key_env_var IS NOT NULL
          GROUP BY api_key_env_var
        ) blocked ON blocked.api_key_env_var = k.env_var
        ORDER BY
          CASE k.status WHEN 'pending' THEN 0 WHEN 'registered' THEN 1 ELSE 2 END,
          k.label
        """
    )


def unregistered_sources() -> list[dict]:
    """Sources that declare they need a key whose variable is not in the
    registry at all. This is the gap the markdown list could never catch: a
    source registered with requires_api_key=true and nobody tracking the key."""
    return fetch_all(
        """
        SELECT s.key, s.name, s.api_key_env_var, e.key AS engine_key, s.status::text AS status
        FROM source s
        JOIN engine e ON e.id = s.engine_id
        WHERE s.requires_api_key
          AND (s.api_key_env_var IS NULL
               OR s.api_key_env_var NOT IN (SELECT env_var FROM ops_console.api_key_registry))
        ORDER BY e.key, s.key
        """
    )


def by_env_var(env_var: str) -> dict | None:
    return fetch_one(
        "SELECT * FROM ops_console.api_key_registry WHERE env_var = %s", (env_var,)
    )


def set_status(key_id: int, status: str) -> None:
    if status not in VALID_STATUS:
        raise ValueError(f"invalid status {status!r}")
    with cursor(commit=True) as cur:
        cur.execute(
            "UPDATE ops_console.api_key_registry SET status = %s WHERE id = %s",
            (status, key_id),
        )


def mark_rotated(env_var: str) -> None:
    """Called only after the control service reports a successful rotation.
    Records the time, never the value."""
    with cursor(commit=True) as cur:
        cur.execute(
            "UPDATE ops_console.api_key_registry "
            "SET last_rotated_at = now(), status = 'registered' WHERE env_var = %s",
            (env_var,),
        )
