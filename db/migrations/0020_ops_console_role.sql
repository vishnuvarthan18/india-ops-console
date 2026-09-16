-- 0020 — the ops_console database role.
--
-- Read-only on every engine table, write only inside the ops_console schema.
-- This is the structural version of the plan's "the web app should never get
-- a raw shell/exec endpoint" rule: even if the console were fully compromised,
-- it could not alter a single fact, entity or run record. Engine data changes
-- only through the core API's validation, and this role cannot bypass that.
--
-- Run as a superuser, and set the password before running:
--   \set ops_password 'the-generated-password'
BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ops_console') THEN
    CREATE ROLE ops_console LOGIN;
  END IF;
END
$$;

GRANT CONNECT ON DATABASE india_data TO ops_console;

-- Read-only everywhere in public.
GRANT USAGE ON SCHEMA public TO ops_console;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ops_console;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO ops_console;

-- Explicitly NOT granted: INSERT, UPDATE, DELETE, TRUNCATE on public.
-- Do not add them later "just for one thing" — that is the whole control.

-- Read/write only inside its own schema.
GRANT USAGE, CREATE ON SCHEMA ops_console TO ops_console;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ops_console TO ops_console;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ops_console TO ops_console;
ALTER DEFAULT PRIVILEGES IN SCHEMA ops_console
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ops_console;
ALTER DEFAULT PRIVILEGES IN SCHEMA ops_console
  GRANT USAGE, SELECT ON SEQUENCES TO ops_console;

COMMIT;

-- Then, separately (so the password never lands in a committed file):
--   ALTER ROLE ops_console PASSWORD 'the-generated-password';
