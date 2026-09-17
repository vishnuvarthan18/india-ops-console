# Tests

Run everything:

    ./scripts/run-tests.sh

That script starts a disposable PostGIS container, applies the real schema
(`india-data-core`'s migrations 0001–0002 plus this repo's 0019–0021), seeds a
fixture, runs pytest, and tears the container down. It needs Docker and nothing
else.

If you already have a database you want to test against:

    TEST_DATABASE_URL=postgresql://user:pw@host:5432/db pytest -v

## What is covered, and why these things

The suite is deliberately weighted towards the parts where a bug is expensive
rather than the parts that are easy to test.

| File | Covers | Why it earns its place |
|---|---|---|
| `test_control_service.py` | the privileged service's allowlist | It runs as root and starts systemd units. This is the highest-consequence code in the repo, so it gets the most hostile tests: command substitution, shell metacharacters, newline and null-byte injection, case and whitespace variation, path traversal, wildcards, and direct attempts at `ssh.service` and `docker.service`. Needs no database. |
| `test_precision.py` | publish_precision on the way out | The database already refuses to *store* sacred groves at full precision. These tests prove the console also refuses to *show or export* them — the read-side half of a legal and ethical rule (PLAN.md §5). |
| `test_auth.py` | who can reach what | Every endpoint, including the JSON and CSV ones, refused when signed out. Rate limiting on repeated failures. |
| `test_pages.py` | every page renders | Cheap, and it catches the template error that only appears with real data shapes. |
| `test_alerting.py` | notification dedup | The whole point of D-70/D-71 was stopping false alarms. A regression here quietly re-creates the noise problem that made alerts worthless. |
| `test_migrations.py` | migrations are re-runnable | A migration that fails halfway on a second run is one nobody dares re-run. |
| `test_queries.py` | staleness logic | Locks in the D-70 fix: a healthy monthly job must not be called stale. |

## What is NOT covered

Stated plainly, because a test suite that hides its gaps is worse than none:

- **Nothing here has run against the production database.** These tests use a
  fresh schema and synthetic rows.
- **No browser tests.** Layout, contrast and keyboard behaviour were verified
  manually with Playwright during development; that check is not automated here.
- **The map's JavaScript is untested.** Only the GeoJSON endpoint behind it is.
- **No load testing.** Measured by hand at ~20k entities / 120k facts: every
  page under 70 ms. Not enforced by a test.
