SET ROLE ip_owner;
ALTER TABLE app_state.runs ADD COLUMN IF NOT EXISTS waiting_review jsonb;
ALTER TABLE app_state.runs ADD COLUMN IF NOT EXISTS resume_payload jsonb;
ALTER TABLE app_state.runs ADD COLUMN IF NOT EXISTS resume_review_id uuid;
ALTER TABLE app_state.confirmations ADD COLUMN IF NOT EXISTS idempotency_key uuid;
ALTER TABLE app_state.confirmations ADD COLUMN IF NOT EXISTS decision_hash text;
CREATE TABLE IF NOT EXISTS app_state.model_calls(
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL, run_id uuid NOT NULL,
 task_revision bigint NOT NULL, execution_version bigint NOT NULL, owner uuid NOT NULL,
 kind text NOT NULL CHECK(kind IN ('plan','sql','describe')),
 reservation int NOT NULL, model_name text NOT NULL,
 status text NOT NULL DEFAULT 'STARTED' CHECK(status IN ('STARTED','SUCCEEDED','FAILED')),
 usage_status text NOT NULL DEFAULT 'unavailable' CHECK(usage_status IN ('reported','unavailable')),
 input_tokens bigint, output_tokens bigint, total_tokens bigint,
 elapsed_ms bigint, error_code text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), ended_at timestamptz,
 UNIQUE(tenant_id,run_id,reservation),
 FOREIGN KEY(tenant_id,run_id) REFERENCES app_state.runs(tenant_id,id),
 CHECK(input_tokens IS NULL OR input_tokens>=0),
 CHECK(output_tokens IS NULL OR output_tokens>=0),
 CHECK(total_tokens IS NULL OR total_tokens>=0));
CREATE TABLE IF NOT EXISTS app_state.agent_reviews(
 id uuid PRIMARY KEY,tenant_id uuid NOT NULL,run_id uuid NOT NULL,user_id uuid NOT NULL,
 task_revision bigint NOT NULL,auth_version bigint NOT NULL,
 checkpoint_digest text NOT NULL,budget jsonb NOT NULL,payload jsonb NOT NULL,
 token_hash text NOT NULL UNIQUE,expires_at timestamptz NOT NULL,
 consumed_at timestamptz,idempotency_key uuid,decision_hash text,receipt jsonb,
 UNIQUE(tenant_id,run_id,id),
 UNIQUE(tenant_id,run_id,task_revision,checkpoint_digest),
 FOREIGN KEY(tenant_id,run_id) REFERENCES app_state.runs(tenant_id,id),
 FOREIGN KEY(tenant_id,user_id) REFERENCES app_state.users(tenant_id,id));
DO $fk$ BEGIN
 IF NOT EXISTS(SELECT FROM pg_constraint WHERE conname='resume_review_owned') THEN
  ALTER TABLE app_state.runs ADD CONSTRAINT resume_review_owned
    FOREIGN KEY(tenant_id,id,resume_review_id) REFERENCES app_state.agent_reviews(tenant_id,run_id,id)
    DEFERRABLE INITIALLY DEFERRED;
 END IF;
END $fk$;
GRANT SELECT,INSERT,UPDATE,DELETE ON app_state.model_calls,app_state.agent_reviews TO ip_state;
RESET ROLE;
