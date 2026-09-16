-- 0021 — alert history, the API key registry, and the control audit log.
--
-- Everything here lives in the ops_console schema, so it stays inside the
-- write boundary established in 0020: the console can manage its own
-- operational state and still cannot touch a fact, an entity or a run.

BEGIN;

-- ---------------------------------------------------------------------------
-- Alert history (plan section 5).
--
-- v_heartbeat_alert answers "what is wrong right now". It cannot answer "what
-- broke last Tuesday and when did it clear", because a resolved alert simply
-- stops appearing. This table is the durable record: one row per alert
-- episode, opened when a condition first appears and closed when it clears.
--
-- The notifier writes here. Notifications fire on TRANSITIONS only — opening a
-- new episode, or closing one — never on every poll, which is what stops a
-- persistent problem generating a message every ten minutes until it is muted
-- and thereby ignored.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops_console.alert_event (
  id           bigserial   PRIMARY KEY,
  alert_key    text        NOT NULL,          -- stable identity, e.g. 'heartbeat:culture-fra-jk'
  kind         text        NOT NULL,          -- 'heartbeat' | 'stale_source' | 'empty_run' | 'rejections' | 'disk' | 'memory'
  severity     text        NOT NULL DEFAULT 'warn',   -- 'warn' | 'critical'
  subject      text        NOT NULL,          -- what it is about (job key, source key, ...)
  detail       text,
  opened_at    timestamptz NOT NULL DEFAULT now(),
  resolved_at  timestamptz,
  notified_at  timestamptz,
  resolve_notified_at timestamptz,
  CONSTRAINT alert_event_severity_valid CHECK (severity IN ('warn', 'critical'))
);

-- One OPEN episode per alert_key at a time. This is the constraint that makes
-- "fires once, not every poll" a property of the schema rather than of the
-- notifier remembering to check.
CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_event_open_unique
  ON ops_console.alert_event(alert_key) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_alert_event_recent
  ON ops_console.alert_event(opened_at DESC);

-- ---------------------------------------------------------------------------
-- API key registry (plan section 7).
--
-- The five pending registrations have been living in a markdown doc, where
-- nothing can check them against reality. Here, `status` is a fact the console
-- displays next to the engines that are blocked by it.
--
-- No secret value is ever stored in this table. It records that a key exists,
-- what environment variable carries it, and who needs it — never the key
-- itself, which lives only in the secrets file on the VPS with 0600
-- permissions.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops_console.api_key_registry (
  id             bigserial   PRIMARY KEY,
  env_var        text        NOT NULL UNIQUE,
  label          text        NOT NULL,
  provider       text,
  engine_key     text,                     -- engine.key this unblocks, if one
  source_keys    text,                     -- comma-separated source.key list
  status         text        NOT NULL DEFAULT 'pending',  -- 'pending' | 'registered' | 'not_needed'
  register_url   text,
  notes          text,
  rotation_days  integer,                  -- NULL when the provider enforces none
  last_rotated_at timestamptz,
  service_unit   text,                     -- systemd unit to restart after rotation
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT api_key_status_valid CHECK (status IN ('pending', 'registered', 'not_needed'))
);

DROP TRIGGER IF EXISTS api_key_registry_touch ON ops_console.api_key_registry;
CREATE TRIGGER api_key_registry_touch
  BEFORE UPDATE ON ops_console.api_key_registry
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------------------------------------------------------------------------
-- Control audit log (plan section 4).
--
-- Every privileged action the console asks the control service to perform is
-- recorded here BEFORE it is attempted, with its outcome written afterwards.
-- An action that crashes the control service therefore still leaves a trace,
-- which an after-the-fact-only log would not.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops_console.control_action (
  id           bigserial   PRIMARY KEY,
  action       text        NOT NULL,       -- 'run_job' | 'rotate_key'
  target       text        NOT NULL,       -- unit name or env var
  requested_at timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  ok           boolean,
  detail       text
);

CREATE INDEX IF NOT EXISTS idx_control_action_recent
  ON ops_console.control_action(requested_at DESC);

-- Seed the registry from what is already known. Additive and safe to re-run.
INSERT INTO ops_console.api_key_registry
  (env_var, label, provider, engine_key, status, register_url, notes, service_unit)
SELECT * FROM (VALUES
  ('WDPA_API_KEY', 'World Database on Protected Areas', 'Protected Planet / UNEP-WCMC',
   'protected_areas', 'pending', 'https://www.protectedplanet.net/en/help-support/developers',
   'Blocks the OSM-vs-WDPA geometry decision. Note WDPA forbids commercial use and redistribution.', NULL),
  ('IUCN_API_KEY', 'IUCN Red List API v4', 'IUCN',
   'living_species', 'pending', 'https://api.iucnredlist.org/',
   'v3 tokens do not work on v4 — a fresh v4 registration is required.', NULL),
  ('GEONAMES_USERNAME', 'GeoNames', 'GeoNames',
   'mountains_geography', 'pending', 'https://www.geonames.org/login',
   'Username acts as the key. Free tier is rate-limited per hour.', NULL),
  ('OPENTOPOGRAPHY_API_KEY', 'OpenTopography', 'OpenTopography',
   'mountains_geography', 'pending', 'https://portal.opentopography.org/requestService',
   'Needed for elevation/DEM access.', NULL),
  ('GFW_API_KEY', 'Global Forest Watch', 'WRI',
   'forests_land', 'pending', 'https://www.globalforestwatch.org/help/developers/',
   'Forest loss/gain time series.', NULL),
  ('DATA_GOV_IN_API_KEY', 'data.gov.in', 'NIC / data.gov.in',
   NULL, 'registered', 'https://data.gov.in/help/how-use-datasets-apis',
   'Registered and working. Flagged for rotation — covers 6 of 8 engines, so treat a leak as platform-wide.',
   NULL)
) AS seed(env_var, label, provider, engine_key, status, register_url, notes, service_unit)
WHERE NOT EXISTS (SELECT 1 FROM ops_console.api_key_registry);

COMMIT;
