-- The previous local migration history proves when those two records were registered.
UPDATE sites SET registered_at = (SELECT replace(applied_at, ' ', 'T') || 'Z' FROM d1_migrations WHERE name = '0002_collected_examples.sql' LIMIT 1)
WHERE id IN ('collected-hyuki','collected-kmonos') AND registered_at IS NULL AND EXISTS (SELECT 1 FROM d1_migrations WHERE name = '0002_collected_examples.sql');
-- New inserts get their actual insertion time when none was provided. Existing rows remain unchanged.
CREATE TRIGGER sites_registration_clock AFTER INSERT ON sites WHEN NEW.registered_at IS NULL BEGIN
  UPDATE sites SET registered_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = NEW.id;
END;
