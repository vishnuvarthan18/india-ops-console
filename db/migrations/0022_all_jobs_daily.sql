-- 0022 — every engine job now runs daily (owner decision 2026-09-23).
-- Tightens the tolerated silence window from weekly/monthly to 36h so a
-- missed daily run is noticed. Idempotent.
BEGIN;
-- Prefix match, not exact keys: heartbeat keys (geo-wikidata-peaks,
-- forest-fsi-isfr, species-gbif-checklists...) differ from systemd unit names,
-- so 0019's exact-name lists missed several jobs. Infra jobs (core-*, ops-*)
-- keep their own cadence.
UPDATE heartbeat SET cadence = 'daily'::schedule_tier
 WHERE job_key ~ '^(geo|forest|forests|culture|species|water|laws|pa|extinct)-';

COMMIT;
