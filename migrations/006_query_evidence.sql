SET ROLE ip_owner;
ALTER TABLE app_state.runs ADD COLUMN IF NOT EXISTS checkpoint_digest text;
ALTER TABLE app_state.runs ADD COLUMN IF NOT EXISTS waiting_query_id uuid;
ALTER TABLE app_state.runs ADD COLUMN IF NOT EXISTS resume_confirmation_id uuid;
ALTER TABLE app_state.query_tickets ADD COLUMN IF NOT EXISTS execution_state text NOT NULL DEFAULT 'READY';
ALTER TABLE app_state.query_tickets ADD COLUMN IF NOT EXISTS execution_version bigint;
ALTER TABLE app_state.query_tickets ADD COLUMN IF NOT EXISTS execution_owner uuid;
ALTER TABLE app_state.query_tickets ADD COLUMN IF NOT EXISTS execution_until timestamptz;
ALTER TABLE app_state.query_tickets ADD COLUMN IF NOT EXISTS error_code text;
ALTER TABLE app_state.confirmations ADD COLUMN IF NOT EXISTS receipt jsonb;
CREATE UNIQUE INDEX IF NOT EXISTS evidence_one_query ON app_state.query_evidence(tenant_id,run_id,query_id);
CREATE UNIQUE INDEX IF NOT EXISTS one_confirmation_snapshot ON app_state.confirmations(tenant_id,run_id,task_revision,query_id,checkpoint_digest);
DO $fk$ BEGIN
 IF NOT EXISTS(SELECT FROM pg_constraint WHERE conname='confirmation_tenant_run_unique') THEN
  ALTER TABLE app_state.confirmations ADD CONSTRAINT confirmation_tenant_run_unique UNIQUE(tenant_id,run_id,id);
 END IF;
 IF NOT EXISTS(SELECT FROM pg_constraint WHERE conname='waiting_query_owned') THEN
  ALTER TABLE app_state.runs ADD CONSTRAINT waiting_query_owned FOREIGN KEY(tenant_id,id,waiting_query_id)
    REFERENCES app_state.query_tickets(tenant_id,run_id,id) DEFERRABLE INITIALLY DEFERRED;
 END IF;
 IF NOT EXISTS(SELECT FROM pg_constraint WHERE conname='resume_confirmation_owned') THEN
  ALTER TABLE app_state.runs ADD CONSTRAINT resume_confirmation_owned FOREIGN KEY(tenant_id,id,resume_confirmation_id)
    REFERENCES app_state.confirmations(tenant_id,run_id,id) DEFERRABLE INITIALLY DEFERRED;
 END IF;
 IF NOT EXISTS(SELECT FROM pg_constraint WHERE conname='query_execution_state_allowed') THEN
  ALTER TABLE app_state.query_tickets ADD CONSTRAINT query_execution_state_allowed CHECK(execution_state IN ('READY','EXECUTING','DONE','FAILED'));
 END IF;
END $fk$;
RESET ROLE;
