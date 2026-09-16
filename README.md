# india-ops-console

The private admin web app for the India data platform. Phase 1: **read-only**.

Covers sections 1, 2, 6, 8 and 9 of `ops-dashboard-plan.md` — overview, engine
health, server and infrastructure, the decisions log, and the open-items
tracker — plus the job-health half of section 4. It also ships the fix for
D-70 (false stale alerts).

There is deliberately **no** endpoint in this app that runs a command, reads a
secret, or writes to an engine table. Manual run triggering, log tailing and
key rotation (plan sections 4, 5 and 7) need a separate privileged control
service; widening this one to do that would throw away the safety properties
described below.

## Why it is safe to expose an admin UI over this data

Three independent controls, none of which relies on the web code being
bug-free:

1. **The database role cannot write engine data.** `ops_console` has `SELECT`
   on everything in `public` and no `INSERT`/`UPDATE`/`DELETE` at all. Its only
   write access is inside the `ops_console` schema, which holds nothing the
   platform's integrity depends on. Verified by test: `DELETE FROM entity_fact`
   as this role returns `permission denied`.
2. **It has no shell and no Docker socket.** The host metrics it displays are
   produced by a root-owned script on the host, on its own timer, written to a
   file the console reads read-only. The web app cannot invoke it.
3. **It is not on the internet.** The port binds to `127.0.0.1` for the same
   reason core-infra's do (D-2: Docker's iptables rules run before ufw's INPUT
   chain, so a `0.0.0.0` bind is internet-reachable even with ufw denying
   everything). Reach it over an SSH tunnel.

On top of those, a real login: single admin account, Argon2id password hash,
signed session cookie. Argon2 rather than SHA-256 because an admin password is
a human-chosen secret and needs a slow hash — unlike the engines' API keys,
which are high-entropy random strings where the core API's SHA-256 is right.

## What D-70 was and what changed

`heartbeat.silence_after` defaulted to 36 hours for every job. Of the 16 engine
jobs confirmed live on 2026-09-16, **8 are monthly and 4 are weekly** — so two
thirds of the fleet was permanently flagged stale between perfectly healthy
runs. An alert list that is always red is one nobody reads.

Migration `0019` adds a declared `cadence` per job (reusing the existing
`schedule_tier` enum) and derives the tolerated silence from it, with an
explicit `silence_after_override` for genuine exceptions. It also stops
treating "registered but not yet due" as a failure — the exact confusion the
2026-09-08 observation pause ran into, when six never-fired jobs looked alarming
but were simply waiting for their first monthly window.

Measured on a test fixture reproducing the real fleet: **6 alerts before, 2
after** — and both survivors were genuine (a daily job silent for 5 days, and a
job registered 200 days ago that has never run).

Every alert now carries a `reason`, so the console shows *why* something is
flagged rather than just listing it.

## Deploying

From your Mac:

    scp -r india-ops-console ubuntu@40.160.137.239:~/

Then on the VPS:

    cd ~/india-ops-console

    # 1. Migrations. 0019 is safely re-runnable; 0020 creates the role.
    docker exec -i core-postgres psql -U india -d india_data < db/migrations/0019_heartbeat_cadence_and_ops_console.sql
    docker exec -i core-postgres psql -U india -d india_data < db/migrations/0020_ops_console_role.sql

    # 2. Give the role a password (kept out of any committed file).
    OPS_PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    docker exec -i core-postgres psql -U india -d india_data \
      -c "ALTER ROLE ops_console PASSWORD '$OPS_PW';"
    echo "$OPS_PW"     # paste into .env as POSTGRES_PASSWORD

    # 3. Configure.
    cp .env.example .env && chmod 600 .env
    python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # SESSION_SECRET
    docker compose build
    docker compose run --rm ops-console python -m app.auth hash     # ADMIN_PASSWORD_HASH
    # edit .env with all three values, plus REPOS_HOST_DIR=/home/ubuntu

    # 4. Host metrics collector.
    sudo mkdir -p /var/ops-metrics
    sudo cp ops/systemd/ops-metrics.* /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now ops-metrics.timer
    sudo systemctl start ops-metrics.service    # first snapshot immediately

    # 5. Start.
    docker compose up -d

Reach it from your Mac:

    ssh -L 8010:127.0.0.1:8010 ubuntu@40.160.137.239
    # then open http://127.0.0.1:8010

## Verification already done (2026-09-16, before deploy)

Run against a throwaway Postgres 16 with the real `0001` and `0002` schema
applied:

- `0019` and `0020` apply cleanly, and `0019` re-runs cleanly three times
  (a missing `DROP TRIGGER IF EXISTS` was caught and fixed here).
- The `ops_console` role can read `entity` and is refused on
  `DELETE FROM entity_fact`.
- The staleness fix: 6 alerts → 2, both genuine.
- Every page returns 200 with real seeded engines and sources: overview,
  alerts, engines, all 8 engine detail pages, server, decisions, open items.
- Signed-out requests redirect to the login form; a wrong password is
  rejected; an unknown engine key 404s; a path-traversal attempt at
  `/decisions/../../etc` is refused; an invalid open-item status is refused
  with 422.

Not yet verified against the live database, since this session had no route
to the VPS.

## Known gaps

- `ops_console.host_metric` exists for metrics history but nothing writes to
  it yet; the console reads the current snapshot from the JSON file. Wire the
  collector to insert there when a trend view is wanted.
- Container memory in the metrics snapshot is always null — `docker stats`
  costs a second per container and the timer runs every 10 minutes. Add it if
  it turns out to matter.
- No map view or data browser (plan section 3) — that is the next phase.
