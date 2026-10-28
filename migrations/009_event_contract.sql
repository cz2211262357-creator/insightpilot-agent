SET ROLE ip_owner;
ALTER TABLE app_state.events ADD COLUMN IF NOT EXISTS execution_version bigint NOT NULL DEFAULT 0;
ALTER TABLE app_state.events ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'BACKGROUND';
CREATE INDEX IF NOT EXISTS events_run_sequence ON app_state.events(tenant_id,run_id,id);
RESET ROLE;
