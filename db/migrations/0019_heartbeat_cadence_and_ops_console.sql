-- 0019 — per-job heartbeat cadence (fixes D-70) + the ops console's own state.
--
-- ## Part 1: D-70, the false-stale problem
--
-- heartbeat.silence_after defaults to '36 hours' for every job. That is right
-- for a daily job and wrong for everything else: a monthly job is silent for
-- ~30 days between perfectly healthy runs, so v_heartbeat_alert has been
-- reporting it as an alert for 28 of every 30 days. An alert list that is
-- always red is an alert list nobody reads, which is worse than no alert list.
--
-- Confirmed live on 2026-09-16: 8 of the 16 engine jobs are monthly and 4 are
-- weekly, so two thirds of the fleet was permanently mis-flagged.
--
-- The fix keeps the existing column (an explicit silence_after still wins, so
-- a job with a genuinely unusual tolerance can override) and adds a declared
-- cadence that derives a sane window when no override is set. Cadence reuses
-- the existing schedule_tier enum rather than inventing a parallel vocabulary.
--
-- The derived window is deliberately generous — roughly two missed runs plus
-- slack — because the cost of a late alert is someone noticing a day later,
-- while the cost of a noisy alert is the whole list being ignored.
--
-- ## Part 2: related fix — "never pinged" is not the same as "broken"
--
-- The old view alerted on last_ping_at IS NULL. A job registered but not yet
-- due has never pinged and is not broken. This distinguishes the two by
-- grace-period from registration, which is exactly the situation the
-- 2026-09-08 observation pause ran into: six jobs that had never fired looked
-- alarming but were simply not due yet.

BEGIN;

ALTER TABLE heartbeat
  ADD COLUMN IF NOT EXISTS cadence schedule_tier,
  ADD COLUMN IF NOT EXISTS silence_after_override interval,
  ADD COLUMN IF NOT EXISTS source_key text;

COMMENT ON COLUMN heartbeat.cadence IS
  'Declared run cadence. Drives the tolerated silence window via '
  'heartbeat_silence_window(). NULL falls back to silence_after.';
COMMENT ON COLUMN heartbeat.silence_after_override IS
  'Explicit per-job tolerance. Wins over the cadence-derived window. Set this '
  'only when a job genuinely needs a non-standard tolerance, and say why in '
  'description.';
COMMENT ON COLUMN heartbeat.source_key IS
  'Optional link to source.key. Heartbeat job keys are systemd unit names '
  '(culture-census-st) while source keys are dataset names (census-2011-nada), '
  'so the two cannot be joined by name — this records the mapping explicitly.';

-- Tolerated silence per cadence: about two missed runs plus slack.
CREATE OR REPLACE FUNCTION heartbeat_silence_window(tier schedule_tier)
RETURNS interval AS $fn$
  SELECT CASE tier
    WHEN 'fifteen_min' THEN interval '90 minutes'
    WHEN 'six_hourly'  THEN interval '18 hours'
    WHEN 'daily'       THEN interval '36 hours'
    WHEN 'weekly'      THEN interval '16 days'
    WHEN 'monthly'     THEN interval '70 days'
    WHEN 'quarterly'   THEN interval '200 days'
    WHEN 'annual'      THEN interval '400 days'
    WHEN 'manual'      THEN NULL          -- never alerts on silence
  END;
$fn$ LANGUAGE sql IMMUTABLE;

-- The effective window for a job, in precedence order:
--   explicit override > cadence-derived > legacy silence_after column.
CREATE OR REPLACE VIEW v_heartbeat_effective AS
SELECT h.*,
       COALESCE(
         h.silence_after_override,
         heartbeat_silence_window(h.cadence),
         h.silence_after
       ) AS effective_silence_after,
       CASE
         WHEN h.silence_after_override IS NOT NULL THEN 'override'
         WHEN h.cadence IS NOT NULL                THEN 'cadence'
         ELSE 'legacy_default'
       END AS window_source
FROM heartbeat h;

-- Rebuilt alert view. Every alert now carries a reason, so the console can
-- show *why* something is flagged instead of just listing it.
DROP VIEW IF EXISTS v_heartbeat_alert;
CREATE VIEW v_heartbeat_alert AS
SELECT job_key, description, last_ping_at, last_status, consecutive_failures,
       now() - last_ping_at AS silence,
       effective_silence_after AS silence_after,
       cadence,
       window_source,
       CASE
         WHEN last_status = 'failed'                        THEN 'last_run_failed'
         WHEN consecutive_failures > 0                      THEN 'consecutive_failures'
         WHEN last_ping_at IS NULL                          THEN 'never_ran'
         ELSE 'silent_past_window'
       END AS reason
FROM v_heartbeat_effective
WHERE last_status = 'failed'
   OR consecutive_failures > 0
   -- Never pinged: only an alert once the job has had a fair chance to run.
   -- A newly registered monthly job is not broken, it is waiting.
   OR (last_ping_at IS NULL
       AND effective_silence_after IS NOT NULL
       AND now() - created_at > effective_silence_after)
   -- Has pinged before, but has now gone quiet past its own window.
   OR (last_ping_at IS NOT NULL
       AND effective_silence_after IS NOT NULL
       AND now() - last_ping_at > effective_silence_after);

-- Declare the cadence of the 16 jobs confirmed live on the VPS 2026-09-16
-- (systemctl list-timers). Jobs not listed here keep the legacy behaviour,
-- so this is additive and safe to re-run.
UPDATE heartbeat SET cadence = 'daily'::schedule_tier
 WHERE job_key IN ('pa-harvest', 'water-nwdp', 'geo-usgs', 'laws-egazette', 'pg-backup', 'core-pg-backup');

UPDATE heartbeat SET cadence = 'weekly'::schedule_tier
 WHERE job_key IN ('species-gbif', 'species-powo', 'geo-peaks',
                   'geo-passes-ranges', 'geo-overpass-peaks-passes');

UPDATE heartbeat SET cadence = 'monthly'::schedule_tier
 WHERE job_key IN ('culture-glot', 'culture-glottolog', 'culture-census-st', 'culture-fra-jk',
                   'forest-fsi', 'forest-worldcover', 'forest-wetlands', 'forest-desertification');

-- ---------------------------------------------------------------------------
-- Part 3: the ops console's own state.
--
-- Its own schema, so the console's read role can be granted write on exactly
-- this and read-only on everything else. The console must never be able to
-- write to engine data — the whole platform's integrity rests on writes going
-- through the core API's validation.
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS ops_console;

-- Section 9 of the plan: the standing "needs the user" list, as real rows
-- instead of a hand-edited markdown doc that drifts.
CREATE TABLE IF NOT EXISTS ops_console.open_item (
  id           bigserial   PRIMARY KEY,
  title        text        NOT NULL,
  detail       text,
  category     text        NOT NULL DEFAULT 'dev',   -- 'needs_user' | 'dev' | 'decision'
  status       text        NOT NULL DEFAULT 'open',  -- 'open' | 'in_progress' | 'done' | 'dropped'
  blocks       text,                                 -- what this is holding up
  sort_order   integer     NOT NULL DEFAULT 100,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  CONSTRAINT open_item_status_valid
    CHECK (status IN ('open', 'in_progress', 'done', 'dropped')),
  CONSTRAINT open_item_category_valid
    CHECK (category IN ('needs_user', 'dev', 'decision'))
);

CREATE INDEX IF NOT EXISTS idx_open_item_status ON ops_console.open_item(status, sort_order);

-- Host metrics history, so section 6 can show a trend and not just a snapshot.
-- Written by the host collector through this same role.
CREATE TABLE IF NOT EXISTS ops_console.host_metric (
  id           bigserial   PRIMARY KEY,
  collected_at timestamptz NOT NULL DEFAULT now(),
  payload      jsonb       NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_host_metric_time ON ops_console.host_metric(collected_at DESC);

-- Seed the standing items from open-items-next-session.md so the tracker is
-- useful the moment it boots rather than starting empty.
INSERT INTO ops_console.open_item (title, detail, category, blocks, sort_order)
SELECT * FROM (VALUES
  ('Register 5 pending API keys',
   'WDPA, IUCN v4, GeoNames, OpenTopography, GFW.',
   'needs_user',
   'Species, Protected Areas and Forests engine work', 10),
  ('Rotate DATA_GOV_IN_API_KEY', NULL, 'needs_user', NULL, 20),
  ('Decide OSM vs WDPA for Protected Areas geometry',
   'Currently parked on OSM, never resolved.',
   'decision', 'Protected Areas geometry completeness', 30),
  ('Decide India Code / Indian Kanoon: build or drop',
   'Blocks laws-engine beyond its single working e-Gazette job.',
   'decision', 'Laws & Management engine', 40),
  ('Capacity call on remaining bulk datasets + India-egress retest',
   'The VPS is in Oregon, USA. India-WRIS and MoTA may be geo-blocked rather than genuinely unreachable — retest from an Indian egress point before writing them off.',
   'needs_user', 'Water Systems and Tribal engine depth', 50),
  ('Commit the FRA J&K exit-code fix to git',
   'Fixed on the VPS 2026-09-16 with sed + image rebuild, but the repo still has the old line, so the next deploy reverts it.',
   'dev', NULL, 60),
  ('Audit other harvesters for the same exit-code bug',
   'grep -rn ''else 2'' ~/*/scripts/*.py — any harvester returning 2 on partial has the same permanent-false-failure behaviour.',
   'dev', NULL, 70),
  ('Extinct Species engine: build or drop',
   'Repo exists on GitHub, deliberately never deployed, 0 jobs running.',
   'decision', NULL, 80),
  ('Build the public-facing website',
   'Everything so far is backend-only.',
   'dev', NULL, 90),
  ('Public API layer', 'Planned in full-platform-architecture-plan.md, not started.', 'dev', NULL, 100),
  ('GitHub Organization move', 'Cosmetic, not urgent.', 'dev', NULL, 200)
) AS seed(title, detail, category, blocks, sort_order)
WHERE NOT EXISTS (SELECT 1 FROM ops_console.open_item);

-- Dropped first so the whole migration stays safely re-runnable: CREATE
-- TRIGGER has no IF NOT EXISTS, and a migration that fails halfway on a
-- second run is a migration nobody dares re-run.
DROP TRIGGER IF EXISTS open_item_touch ON ops_console.open_item;
CREATE TRIGGER open_item_touch
  BEFORE UPDATE ON ops_console.open_item
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

COMMIT;
