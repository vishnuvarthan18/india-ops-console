# india-ops-console

The private admin web app for the India data platform. All nine sections of
`ops-dashboard-plan.md` are built.

Overview, engine health, data browser with map, run control, alerts with
history and notifications, server and infrastructure, API key registry, the
decisions log, and the open-items tracker. It also ships the fix for D-70
(false stale alerts).

The web app still runs no commands itself. Actions — starting a job, tailing a
log, rotating a key — go to `ops-control`, a separate ~300-line service that
runs on the host with a strict allowlist. That split is what lets the console
gain real powers without the web layer ever getting a shell.

## Why it is safe to expose an admin UI over this data

Four independent controls, none of which relies on the web code being
bug-free:

1. **The database role cannot write engine data.** `ops_console` has `SELECT`
   on everything in `public` and no `INSERT`/`UPDATE`/`DELETE` at all. Its only
   write access is inside the `ops_console` schema, which holds nothing the
   platform's integrity depends on. Verified by test: `DELETE FROM entity_fact`
   as this role returns `permission denied`.
2. **It has no shell and no Docker socket.** The host metrics it displays are
   produced by a root-owned script on the host, on its own timer, written to a
   file the console reads read-only. The web app cannot invoke it.
2b. **Actions go through an allowlist the console cannot edit.** `ops-control`
   compares every requested unit name against `/etc/ops-control/units.allow`
   and every key name against `keyvars.allow` — files owned by root, outside
   the container. It has three verbs (start a unit, read its status, tail its
   journal) plus a single-line secrets write. Every subprocess call passes an
   argument list with `shell=False`, so there is no string a request can
   influence that a shell ever parses. Tested against command substitution,
   shell metacharacters, null bytes, newline injection, case variation,
   whitespace padding and path traversal — all refused, all 403.
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

## How each phase-2 piece works

### Data browser and map (section 3)

Filters are built from a fixed column map in `browse.py`, never from request
strings: a parameter the map does not know is dropped, so no query string
reaches SQL as text.

`publish_precision` is honoured on the way *out*, not only on the way in. The
database already refuses to store a sacred grove, traditional-knowledge record
or FRA claim at full precision. This applies the matching read-side rule:
restricted entities appear on the map at a district-level representative point
(dashed amber, labelled "shown at district level"), and CSV exports drop their
coordinates entirely while withheld entities are excluded outright. Stricter
than an internal tool strictly needs — but an export is a file that leaves the
platform the moment someone forwards it, and §5's line is about the data, not
the viewer. Verified: 8 test groves collapse to 3 distinct district points, and
their CSV latitude column is empty.

Leaflet is **vendored** into `app/static/vendor/` (BSD-2, licence included)
rather than loaded from a CDN, because the console must render with no outbound
network. Map tiles come from OpenStreetMap over the *viewer's* browser
connection, with attribution; the server itself still makes no outbound
requests. Entity names are escaped before going into popups — a name arrives
from a government CSV, not from a trusted author.

The map plots points only. Polygons are invisible at national zoom and cost
megabytes; the detail page has the real geometry for a single entity. The
geometry-coverage table beneath the map is the honest companion to it: it says
what fraction of each entity type has coordinates at all, so the map is not
mistaken for the whole platform.

### Run control (section 4)

`/jobs/<unit>` shows status and the recent journal; one button starts it.
Everything routes through `ops-control`. `systemctl start --no-block` is used
so a multi-minute harvest does not hold an HTTP request open — the page polls
instead.

Every action is written to `ops_console.control_action` **before** it is
attempted, so an action that kills the control service still leaves a trace.

### Alerts (section 5)

The existing views answer "what is wrong now" and forget a problem the instant
it clears. `ops_console.alert_event` turns each condition into an episode with
an `opened_at` and a `resolved_at`, so "what broke last Tuesday and when did it
clear" is answerable a month later.

Notifications fire on **transitions only** — an episode opening or closing —
never on every poll. A partial unique index on open episodes enforces that
structurally, so the notifier does not have to remember what it already sent.
Verified: three consecutive polls of the same nine conditions produced nine
messages, not twenty-seven. Resolutions are notified too; a channel that only
ever delivers bad news gets muted.

Delivery failure never breaks the history, and the notifier exits 0 even when a
webhook is down — an alerting system that becomes a failed unit because a
webhook is down is the same noise loop D-70 was.

### API keys (section 7)

No key value is ever stored or read back. The registry records that a key
exists, which variable carries it, who is blocked without it, and when it was
last rotated. Rotation hands the value straight to the control service, which
rewrites one line of the secrets file atomically at 0600, keeps a `.bak`, and
optionally restarts one allowlisted service. The value is never logged, never
stored, and never rendered.

`keyvars.allow` deliberately excludes `POSTGRES_PASSWORD`,
`MINIO_ROOT_PASSWORD`, `SESSION_SECRET` and `CONTROL_TOKEN`: rotating those
from a web form would let one compromised session take the platform apart, and
they are rare enough to do by hand.

The page also lists sources that declare `requires_api_key` but whose variable
is not in the registry — the gap a hand-maintained markdown list cannot catch.

## Deploying phase 2

In addition to the phase-1 steps:

    # migrations
    docker exec -i core-postgres psql -U india -d india_data < db/migrations/0021_ops_console_phase2.sql

    # control service (host, not a container)
    sudo mkdir -p /etc/ops-control
    sudo cp control/units.allow control/keyvars.allow /etc/ops-control/
    sudo cp control/ops-control.env.example /etc/ops-control/ops-control.env
    sudo chmod 600 /etc/ops-control/ops-control.env
    python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # CONTROL_TOKEN
    # put that token in BOTH /etc/ops-control/ops-control.env and the console's .env
    sudo cp control/ops-control.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now ops-control

    # alert notifier timer
    sudo cp ops/systemd/ops-alerts.* /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now ops-alerts.timer

    docker compose up -d --build

Review `control/units.allow` before installing it. That file is the security
boundary: a unit not listed cannot be touched by the console whatever the
request says.

## Tests

    ./scripts/run-tests.sh

**184 tests, ~9 seconds.** The script starts a disposable PostGIS container,
applies the real schema, seeds a fixture, runs pytest and tears the container
down. `tests/README.md` says what each file covers and, just as importantly,
what it does not.

85 of those tests — including the whole control-service suite, which is the
highest-consequence code here — need no database and run anywhere:

    pytest          # database-backed tests skip with a clear message

### What writing the suite found

Four bugs, three of them in the tests rather than the app, which is itself
worth knowing:

- **Real:** the session epoch was stamped from a module-level settings snapshot
  taken at import, while the check that validates it read settings fresh. In
  production the process restarts so the two agreed; under test they did not.
  Both now read the same source.
- The leak test for withheld coordinates matched an inline SVG icon's path data
  (`m10.2 10.2 3.3 3.3`) and cried wolf. It now reads the page's visible text.
- Migration tests ran as the console's own restricted role, which correctly
  cannot ALTER TABLE — they run as the superuser now. The restriction working
  is the point, tested separately.
- A test asserting that proxy headers are ignored matched the docstring that
  explains why they are ignored. It now parses the function body.

## Phase-2 verification (2026-09-16, before deploy)

Against a throwaway Postgres 16 **with PostGIS**, real `0001`/`0002` schema,
153 seeded entities including 8 restricted sacred groves:

- All 9 pages plus every filter combination return 200.
- Restricted entities: coordinates hidden on the detail page, collapsed to 3
  district points on the map, empty latitude in CSV; full-precision rows keep
  their coordinates.
- The map page loads no script or stylesheet from the internet.
- Control service refused all 14 hostile unit names (command substitution,
  metacharacters, newline injection, null byte, case variation, whitespace
  padding, traversal, wildcard, `docker.service`, `ssh.service`, non-`.service`
  suffix) and accepted only the exact allowlisted names.
- Rotation refused `POSTGRES_PASSWORD`, `MINIO_ROOT_PASSWORD`, `CONTROL_TOKEN`
  and `SESSION_SECRET`; refused short and non-string values; refused to restart
  a non-allowlisted unit; wrote 0600 with a `.bak`; and no key value appeared
  in any log.
- Notifier: 9 opens then two silent polls; resolving one condition sent exactly
  one resolve message; a dead webhook still recorded the episode and exited 0.
- Every new endpoint — geojson, CSV, job trigger, key rotation — refuses a
  signed-out request.

Still not verified against the live database.

## Production readiness

Honest status: **not yet deployed, and nothing here has run against the
production database.** Every test uses a fresh schema and synthetic rows.

Closed since the first review:

- A committed test suite, so the verification is repeatable by anyone.
- Login throttling: five failed attempts locks that client out for five
  minutes, and a locked-out client cannot get in even with the right password.
  A correct sign-in clears the count.
- A second hardening pass on the control service: request and path size limits,
  control characters refused before the allowlist comparison (so a refused name
  cannot forge log lines), and line breaks refused in a rotation value — which
  would otherwise have let one rotation define a second variable in the secrets
  file, including one the service refuses to rotate directly.
- Session revocation via `SESSION_EPOCH`, without changing the signing secret.
- A real error page instead of a bare JSON body, which never echoes the
  underlying message — an error can carry a connection string.

Still open:

- **Deploy and verify against the live database.** Nothing replaces this.
- **The base image is not pinned.** Run `./scripts/pin-base-image.sh` on a
  machine with Docker before the first production deploy; the session that
  wrote this had no route to a registry and would not commit a digest it could
  not verify.
- **No second pair of eyes on `control/control_service.py`.** It is ~300 lines,
  runs as root, and has been reviewed only by its author. If one file gets an
  external review, it is that one.
- **No browser-level tests.** Layout, contrast and keyboard behaviour were
  checked manually with Playwright; that check is not automated.

## Known gaps

- `ops_console.host_metric` exists for metrics history but nothing writes to
  it yet; the console reads the current snapshot from the JSON file. Wire the
  collector to insert there when a trend view is wanted.
- Container memory in the metrics snapshot is always null — `docker stats`
  costs a second per container and the timer runs every 10 minutes.
- The job page does not auto-refresh while a job runs; reload to see progress.
- Retry is the same action as run (harvests are idempotent), so there is no
  separate retry button.
