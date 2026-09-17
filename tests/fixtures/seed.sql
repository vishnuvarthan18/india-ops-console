-- Test fixture: the smallest dataset that exercises every rule the console has.
--
-- Deliberately includes the awkward shapes, because those are where bugs live:
--   * a restricted entity type (sacred_grove), which the database caps at
--     district precision and the console must not reveal
--   * records with no geometry at all, so coverage maths meets a zero
--   * a run that "succeeded" while accepting nothing — the failure that hides
--   * a failed run, a partial run, and a job that has never reported

BEGIN;

-- Protected areas: ordinary, full-precision records with real geometry.
INSERT INTO entity (uid, engine_id, entity_type, slug, name, state, district,
                    geom, centroid, publish_precision)
SELECT 'pa:protected_area:res-' || g,
       (SELECT id FROM engine WHERE key = 'protected_areas'),
       'protected_area', 'res-' || g, 'Test Reserve ' || g,
       (ARRAY['Karnataka','Kerala','Assam','Odisha'])[1 + (g % 4)],
       'District ' || (g % 7),
       ST_SetSRID(ST_MakePoint(76 + (g % 20) * 0.3, 12 + (g % 15) * 0.4), 4326),
       ST_SetSRID(ST_MakePoint(76 + (g % 20) * 0.3, 12 + (g % 15) * 0.4), 4326),
       'full'
FROM generate_series(1, 120) g;

-- Sacred groves: the database's own trigger forbids 'full' here. They share
-- three districts, so a correct map collapses eight records to three points.
INSERT INTO entity (uid, engine_id, entity_type, slug, name, state, district,
                    geom, centroid, publish_precision)
SELECT 'culture:sacred_grove:sg-' || g,
       (SELECT id FROM engine WHERE key = 'tribal_culture'),
       'sacred_grove', 'sg-' || g, 'Sacred Grove ' || g,
       'Kerala', 'District ' || (g % 3),
       ST_SetSRID(ST_MakePoint(76.5 + g * 0.01, 10.2 + g * 0.01), 4326),
       ST_SetSRID(ST_MakePoint(76.5 + g * 0.01, 10.2 + g * 0.01), 4326),
       'district_aggregate'
FROM generate_series(1, 8) g;

-- Wetlands with no geometry, so geometry-coverage maths meets a real zero.
INSERT INTO entity (uid, engine_id, entity_type, slug, name, state, publish_precision)
SELECT 'forest:wetland:w-' || g,
       (SELECT id FROM engine WHERE key = 'forests_land'),
       'wetland', 'w-' || g, 'Test Wetland ' || g, 'Assam', 'full'
FROM generate_series(1, 25) g;

-- A fact per entity, with mixed confidence so read-time resolution has work.
INSERT INTO entity_fact (entity_id, field_name, value_num, unit, source_id, retrieved_at,
                         confidence, license, publish_precision, content_hash, created_at)
SELECT e.id, 'area_sq_km', 50 + (e.id % 400), 'sq_km',
       (SELECT id FROM source LIMIT 1), now(),
       (ARRAY['high','medium','low'])[1 + (e.id % 3)]::confidence_level,
       'CC-BY-4.0', e.publish_precision, md5(e.id::text || 'a'),
       now() - make_interval(days => (e.id % 25)::int)
FROM entity e;

-- Runs, including run 5 which succeeds while accepting nothing.
INSERT INTO harvest_run (source_id, engine_id, run_key, status, started_at, finished_at,
                         records_seen, records_accepted)
SELECT (SELECT id FROM source LIMIT 1),
       (SELECT id FROM engine WHERE key = 'protected_areas'),
       'fixture-run-' || g,
       (ARRAY['success','success','partial','failed'])[1 + (g % 4)]::run_status,
       now() - make_interval(hours => g),
       now() - make_interval(hours => g) + interval '30 seconds',
       100, CASE WHEN g = 5 THEN 0 ELSE 90 END
FROM generate_series(1, 15) g;

-- Heartbeats covering healthy, late and failed.
INSERT INTO heartbeat (job_key, cadence, last_ping_at, last_status, created_at) VALUES
  ('culture-census-st', 'monthly', now() - interval '20 days', 'success', now() - interval '90 days'),
  ('pa-harvest',        'daily',   now() - interval '6 hours', 'success', now() - interval '90 days'),
  ('water-nwdp',        'daily',   now() - interval '5 days',  'success', now() - interval '90 days'),
  ('forest-fsi',        'monthly', now() - interval '2 days',  'failed',  now() - interval '90 days')
ON CONFLICT (job_key) DO NOTHING;

COMMIT;
