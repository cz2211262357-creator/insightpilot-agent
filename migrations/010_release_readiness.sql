SET ROLE ip_owner;
ALTER TABLE app_state.model_calls ADD COLUMN IF NOT EXISTS provider_model text;
ALTER TABLE app_state.model_calls DROP CONSTRAINT IF EXISTS model_calls_usage_status_check;
ALTER TABLE app_state.model_calls ADD CONSTRAINT model_calls_usage_status_check
 CHECK(usage_status IN ('reported','partial','unavailable'));
CREATE TABLE IF NOT EXISTS app_state.schema_version(
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),version int NOT NULL);
INSERT INTO app_state.schema_version(singleton,version) VALUES(true,10)
 ON CONFLICT(singleton) DO UPDATE SET version=EXCLUDED.version;
GRANT SELECT ON app_state.schema_version TO ip_state;
RESET ROLE;
