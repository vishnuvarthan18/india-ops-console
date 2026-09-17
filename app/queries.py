"""Every SQL query the console runs, in one place.

All read-only against the engine tables. Kept here rather than inlined in the
route handlers so that the exact shape of what the console asks the database is
reviewable in a single file — useful when the schema moves under it.
"""

from app.db import fetch_all, fetch_one

# ---------------------------------------------------------------------------
# Section 1 — overview
# ---------------------------------------------------------------------------

def totals() -> dict:
    return fetch_one(
        """
        SELECT
          (SELECT count(*) FROM entity)                          AS entities,
          (SELECT count(*) FROM entity_fact)                     AS facts,
          (SELECT count(*) FROM taxon)                           AS taxa,
          (SELECT count(*) FROM occurrence)                      AS occurrences,
          (SELECT count(*) FROM source)                          AS sources,
          (SELECT count(*) FROM source WHERE status = 'active')  AS sources_active,
          (SELECT count(*) FROM engine)                          AS engines,
          (SELECT count(DISTINCT engine_id) FROM entity)         AS engines_with_data
        """
    ) or {}


def alert_counts() -> dict:
    """Counts behind the red/yellow/green light.

    'empty successful runs' is weighted as heavily as a hard failure on
    purpose: a harvester that returns zero rows without throwing is the
    failure mode the platform was explicitly designed to catch.
    """
    return fetch_one(
        """
        SELECT
          (SELECT count(*) FROM v_heartbeat_alert)  AS heartbeat_alerts,
          (SELECT count(*) FROM v_stale_source)     AS stale_sources,
          (SELECT count(*) FROM harvest_run
             WHERE status = 'success' AND records_accepted = 0
               AND finished_at > now() - interval '30 days') AS empty_successful_runs,
          (SELECT count(*) FROM ingest_rejection
             WHERE replayed_at IS NULL)            AS open_rejections
        """
    ) or {}


def recent_runs(limit: int = 25) -> list[dict]:
    return fetch_all(
        """
        SELECT r.id, r.run_key, r.status::text AS status, r.started_at, r.finished_at,
               r.records_seen, r.records_accepted, r.records_rejected, r.error_message,
               s.key AS source_key, s.name AS source_name,
               e.key AS engine_key, e.name AS engine_name,
               EXTRACT(EPOCH FROM (r.finished_at - r.started_at)) AS duration_s
        FROM harvest_run r
        JOIN source s ON s.id = r.source_id
        JOIN engine e ON e.id = r.engine_id
        ORDER BY r.started_at DESC
        LIMIT %s
        """,
        (limit,),
    )


def growth_last_30_days() -> list[dict]:
    """Facts written per day. The honest signal that the platform is alive —
    a flat line here means every job is silently doing nothing."""
    return fetch_all(
        """
        SELECT d::date AS day, COALESCE(n, 0) AS facts
        FROM generate_series(now()::date - 29, now()::date, interval '1 day') AS d
        LEFT JOIN (
          SELECT created_at::date AS day, count(*) AS n
          FROM entity_fact
          WHERE created_at > now() - interval '30 days'
          GROUP BY 1
        ) t ON t.day = d::date
        ORDER BY d
        """
    )


# ---------------------------------------------------------------------------
# Section 2 — engine health
# ---------------------------------------------------------------------------

def engines() -> list[dict]:
    return fetch_all(
        """
        SELECT e.id, e.key, e.name, e.description,
               COALESCE(ent.n, 0)      AS entities,
               COALESCE(f.n, 0)        AS facts,
               COALESCE(src.n, 0)      AS sources,
               COALESCE(src.n_active, 0) AS sources_active,
               last_run.finished_at    AS last_run_at,
               last_run.status::text   AS last_run_status
        FROM engine e
        LEFT JOIN (SELECT engine_id, count(*) n FROM entity GROUP BY 1) ent
               ON ent.engine_id = e.id
        LEFT JOIN (SELECT en.engine_id, count(*) n
                     FROM entity_fact ef JOIN entity en ON en.id = ef.entity_id
                    GROUP BY 1) f
               ON f.engine_id = e.id
        LEFT JOIN (SELECT engine_id, count(*) n,
                          count(*) FILTER (WHERE status = 'active') n_active
                     FROM source GROUP BY 1) src
               ON src.engine_id = e.id
        LEFT JOIN LATERAL (
              SELECT r.finished_at, r.status
              FROM harvest_run r
              WHERE r.engine_id = e.id AND r.finished_at IS NOT NULL
              ORDER BY r.finished_at DESC LIMIT 1
        ) last_run ON true
        ORDER BY e.id
        """
    )


def engine_by_key(key: str) -> dict | None:
    return fetch_one("SELECT id, key, name, description FROM engine WHERE key = %s", (key,))


def engine_sources(engine_id: int) -> list[dict]:
    """Per-source detail, including the two things that are easy to lose track
    of: what licence the data came under, and when it was last successfully
    fetched."""
    return fetch_all(
        """
        SELECT s.id, s.key, s.name, s.base_url, s.kind::text AS kind,
               s.status::text AS status,
               s.schedule_tier::text AS schedule_tier,
               s.staleness_ceiling,
               s.license_default, s.license_url, s.attribution,
               s.commercial_use_allowed, s.redistribution_allowed,
               s.publish_precision_default::text AS publish_precision_default,
               s.requires_api_key, s.api_key_env_var, s.notes,
               last.finished_at AS last_success_at,
               last.records_accepted AS last_records_accepted,
               COALESCE(fc.n, 0) AS facts
        FROM source s
        LEFT JOIN LATERAL (
              SELECT r.finished_at, r.records_accepted
              FROM harvest_run r
              WHERE r.source_id = s.id AND r.status IN ('success', 'partial')
              ORDER BY r.finished_at DESC NULLS LAST LIMIT 1
        ) last ON true
        LEFT JOIN (SELECT source_id, count(*) n FROM entity_fact GROUP BY 1) fc
               ON fc.source_id = s.id
        WHERE s.engine_id = %s
        ORDER BY s.status, s.key
        """,
        (engine_id,),
    )


def engine_entity_types(engine_id: int) -> list[dict]:
    return fetch_all(
        """
        SELECT entity_type, count(*) AS n,
               count(*) FILTER (WHERE geom IS NOT NULL) AS with_geometry
        FROM entity WHERE engine_id = %s
        GROUP BY entity_type ORDER BY n DESC
        """,
        (engine_id,),
    )


def engine_runs(engine_id: int, limit: int = 20) -> list[dict]:
    return fetch_all(
        """
        SELECT r.id, r.status::text AS status, r.started_at, r.finished_at,
               r.records_seen, r.records_accepted, r.error_message,
               s.key AS source_key
        FROM harvest_run r JOIN source s ON s.id = r.source_id
        WHERE r.engine_id = %s
        ORDER BY r.started_at DESC LIMIT %s
        """,
        (engine_id, limit),
    )


# ---------------------------------------------------------------------------
# Section 4 (read-only half) — job health
# ---------------------------------------------------------------------------

def jobs() -> list[dict]:
    """Every heartbeat job with its real, cadence-aware tolerance.

    `is_alerting` here matches v_heartbeat_alert exactly rather than
    re-deriving it — one definition of "is this job in trouble", not two that
    can drift apart.
    """
    return fetch_all(
        """
        SELECT h.job_key, h.description, h.cadence::text AS cadence,
               h.last_ping_at, h.last_status::text AS last_status,
               h.last_detail, h.consecutive_failures, h.created_at,
               h.effective_silence_after, h.window_source,
               now() - h.last_ping_at AS silence,
               a.reason AS alert_reason,
               (a.job_key IS NOT NULL) AS is_alerting
        FROM v_heartbeat_effective h
        LEFT JOIN v_heartbeat_alert a ON a.job_key = h.job_key
        ORDER BY (a.job_key IS NOT NULL) DESC, h.job_key
        """
    )


def alerts_detail() -> dict:
    return {
        "heartbeats": fetch_all("SELECT * FROM v_heartbeat_alert ORDER BY reason, job_key"),
        "stale_sources": fetch_all("SELECT * FROM v_stale_source ORDER BY age DESC NULLS FIRST"),
        "empty_successful_runs": fetch_all(
            """
            SELECT r.id, s.key AS source_key, r.finished_at, r.records_seen, r.records_accepted
            FROM harvest_run r JOIN source s ON s.id = r.source_id
            WHERE r.status = 'success' AND r.records_accepted = 0
              AND r.finished_at > now() - interval '30 days'
            ORDER BY r.finished_at DESC LIMIT 50
            """
        ),
        "open_rejections": fetch_all(
            """
            SELECT reason_code, count(*) AS n, max(received_at) AS latest
            FROM ingest_rejection WHERE replayed_at IS NULL
            GROUP BY reason_code ORDER BY n DESC
            """
        ),
    }


# ---------------------------------------------------------------------------
# Section 6 — database side of server/infrastructure
# ---------------------------------------------------------------------------

def db_size() -> dict:
    return fetch_one(
        "SELECT pg_database_size(current_database()) AS bytes, "
        "pg_size_pretty(pg_database_size(current_database())) AS pretty"
    ) or {}


def largest_tables(limit: int = 15) -> list[dict]:
    return fetch_all(
        """
        SELECT c.relname AS table_name,
               pg_total_relation_size(c.oid)               AS bytes,
               pg_size_pretty(pg_total_relation_size(c.oid)) AS pretty,
               c.reltuples::bigint                          AS approx_rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY pg_total_relation_size(c.oid) DESC
        LIMIT %s
        """,
        (limit,),
    )


def latest_host_metrics() -> dict | None:
    row = fetch_one(
        "SELECT collected_at, payload FROM ops_console.host_metric "
        "ORDER BY collected_at DESC LIMIT 1"
    )
    return row


def host_metric_history(hours: int = 48) -> list[dict]:
    return fetch_all(
        """
        SELECT collected_at, payload
        FROM ops_console.host_metric
        WHERE collected_at > now() - make_interval(hours => %s)
        ORDER BY collected_at
        """,
        (hours,),
    )


# ---------------------------------------------------------------------------
# Section 9 — open items
# ---------------------------------------------------------------------------

def open_items() -> list[dict]:
    return fetch_all(
        """
        SELECT id, title, detail, category, status, blocks, sort_order,
               created_at, updated_at, completed_at
        FROM ops_console.open_item
        ORDER BY
          CASE status WHEN 'in_progress' THEN 0 WHEN 'open' THEN 1
                      WHEN 'done' THEN 2 ELSE 3 END,
          CASE category WHEN 'needs_user' THEN 0 WHEN 'decision' THEN 1 ELSE 2 END,
          sort_order, id
        """
    )


# ---------------------------------------------------------------------------
# The overview's "needs attention" list
# ---------------------------------------------------------------------------

_REASON_LABEL = {
    "last_run_failed":      "last run failed",
    "consecutive_failures": "failing repeatedly",
    "never_ran":            "never ran",
    "silent_past_window":   "overdue",
}


def attention(limit: int = 8) -> list[dict]:
    """One ranked list of everything a person should act on, worst first.

    Deliberately merges heartbeat alerts and silently-empty runs into a single
    list rather than showing two competing tables. Someone arriving at this
    console wants one answer to "what do I do now", not a choice of which
    problem taxonomy to read first.

    Each row carries the systemd unit where one exists, so the fix — inspect
    the log, run it again — is a button on this row instead of a trip to a
    terminal.
    """
    rows: list[dict] = []

    for r in fetch_all(
        """
        SELECT job_key, reason, last_status::text AS last_status, last_ping_at,
               consecutive_failures, cadence::text AS cadence
        FROM v_heartbeat_alert
        """
    ):
        critical = r["reason"] in ("last_run_failed", "consecutive_failures")
        rows.append({
            "subject": r["job_key"],
            "unit": f"{r['job_key']}.service",
            "link": None,
            "kind_label": f"{r['cadence'] or 'undeclared'} job",
            "reason_label": _REASON_LABEL.get(r["reason"], r["reason"]),
            "severity": "critical" if critical else "warn",
            "sort": 0 if critical else 2,
            "when": r["last_ping_at"].strftime("%d %b %H:%M") if r["last_ping_at"] else "never",
        })

    for r in fetch_all(
        """
        SELECT s.key AS source_key, e.key AS engine_key, e.name AS engine_name,
               max(r.finished_at) AS latest, count(*) AS n
        FROM harvest_run r
        JOIN source s ON s.id = r.source_id
        JOIN engine e ON e.id = s.engine_id
        WHERE r.status = 'success' AND r.records_accepted = 0
          AND r.finished_at > now() - interval '30 days'
        GROUP BY s.key, e.key, e.name
        """
    ):
        rows.append({
            "subject": r["source_key"],
            "unit": None,
            "link": f"/engines/{r['engine_key']}",
            "kind_label": f"{r['engine_name']} source",
            "reason_label": f"{r['n']} run(s) collected nothing",
            "severity": "critical",
            "sort": 1,
            "when": r["latest"].strftime("%d %b %H:%M") if r["latest"] else "—",
        })

    for r in fetch_all("SELECT key, engine, age, last_success FROM v_stale_source"):
        rows.append({
            "subject": r["key"],
            "unit": None,
            "link": f"/engines/{r['engine']}",
            "kind_label": "data source",
            "reason_label": "past its staleness limit",
            "severity": "warn",
            "sort": 3,
            "when": r["last_success"].strftime("%d %b %H:%M") if r["last_success"] else "never",
        })

    rows.sort(key=lambda x: (x["sort"], x["subject"]))
    return rows[:limit]


def job_count() -> int:
    row = fetch_one("SELECT count(*) AS n FROM heartbeat")
    return (row or {}).get("n", 0)
