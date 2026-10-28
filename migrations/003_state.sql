SET ROLE ip_owner;
CREATE TABLE IF NOT EXISTS app_state.tenants(id uuid PRIMARY KEY, name text NOT NULL);
CREATE TABLE IF NOT EXISTS app_state.dataset_versions(
 tenant_id uuid NOT NULL REFERENCES app_state.tenants, id uuid NOT NULL, seed bigint NOT NULL,
 sha256 text NOT NULL, catalog_version text NOT NULL, as_of timestamptz NOT NULL,
 active bool NOT NULL DEFAULT true, PRIMARY KEY(tenant_id,id));
CREATE UNIQUE INDEX IF NOT EXISTS dataset_one_active ON app_state.dataset_versions(tenant_id) WHERE active;
CREATE TABLE IF NOT EXISTS app_state.users(
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES app_state.tenants,
 email text NOT NULL, display_name text NOT NULL, password_hash text NOT NULL,
 role text NOT NULL CHECK(role IN ('admin','analyst')), enabled bool NOT NULL DEFAULT true,
 auth_version bigint NOT NULL DEFAULT 1 CHECK(auth_version>0), created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,email));
CREATE TABLE IF NOT EXISTS app_state.sessions(
 token_hash text PRIMARY KEY, tenant_id uuid NOT NULL, user_id uuid NOT NULL,
 auth_version bigint NOT NULL, csrf_hash text NOT NULL, expires_at timestamptz NOT NULL,
 FOREIGN KEY(tenant_id,user_id) REFERENCES app_state.users(tenant_id,id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS app_state.conversations(
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL, user_id uuid NOT NULL, epoch bigint NOT NULL DEFAULT 0,
 active_topic_id uuid, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(tenant_id,id), FOREIGN KEY(tenant_id,user_id) REFERENCES app_state.users(tenant_id,id));
CREATE TABLE IF NOT EXISTS app_state.topics(
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL, conversation_id uuid NOT NULL, title text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(tenant_id,id),
 UNIQUE(tenant_id,conversation_id,id),
 FOREIGN KEY(tenant_id,conversation_id) REFERENCES app_state.conversations(tenant_id,id));
DO $fk$ BEGIN
 IF NOT EXISTS(SELECT FROM pg_constraint WHERE conname='active_topic_owned') THEN
 ALTER TABLE app_state.conversations ADD CONSTRAINT active_topic_owned
 FOREIGN KEY(tenant_id,id,active_topic_id) REFERENCES app_state.topics(tenant_id,conversation_id,id) DEFERRABLE INITIALLY DEFERRED;
 END IF;
END $fk$;
CREATE TABLE IF NOT EXISTS app_state.runs(
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL, conversation_id uuid NOT NULL,
 topic_id uuid NOT NULL, user_id uuid NOT NULL, dataset_version uuid NOT NULL,
 auth_version bigint NOT NULL, question text NOT NULL, scope text NOT NULL CHECK(scope IN ('INTERACTIVE','BACKGROUND')),
 epoch bigint NOT NULL, task_revision bigint NOT NULL DEFAULT 1, execution_version bigint NOT NULL DEFAULT 0,
 thread_id uuid NOT NULL UNIQUE, supersedes_run_id uuid,
 state text NOT NULL DEFAULT 'QUEUED' CHECK(state IN ('QUEUED','RUNNING','WAITING_INPUT','SUCCEEDED','FAILED','TIMED_OUT','CANCELLED','SUPERSEDED')),
 owner uuid, lease_until timestamptz, queue_until timestamptz NOT NULL,
 available_at timestamptz NOT NULL DEFAULT clock_timestamp(), active_deadline timestamptz,
 remaining_seconds numeric(12,3) NOT NULL CHECK(remaining_seconds>=0),
 attempts int NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3), total_claims int NOT NULL DEFAULT 0,
 model_used int NOT NULL DEFAULT 0 CHECK(model_used BETWEEN 0 AND 8),
 query_used int NOT NULL DEFAULT 0 CHECK(query_used BETWEEN 0 AND 4),
 repair_used int NOT NULL DEFAULT 0 CHECK(repair_used BETWEEN 0 AND 2),
 report jsonb, error_code text, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), ended_at timestamptz,
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,topic_id,id),
 FOREIGN KEY(tenant_id,conversation_id,topic_id) REFERENCES app_state.topics(tenant_id,conversation_id,id),
 FOREIGN KEY(tenant_id,user_id) REFERENCES app_state.users(tenant_id,id),
 FOREIGN KEY(tenant_id,dataset_version) REFERENCES app_state.dataset_versions(tenant_id,id),
 FOREIGN KEY(tenant_id,supersedes_run_id) REFERENCES app_state.runs(tenant_id,id));
CREATE INDEX IF NOT EXISTS runs_dispatch ON app_state.runs(state,available_at,created_at) WHERE state IN ('QUEUED','RUNNING');
CREATE TABLE IF NOT EXISTS app_state.messages(
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL, conversation_id uuid NOT NULL, topic_id uuid NOT NULL,
 run_id uuid, client_message_id uuid, body_hash text, receipt jsonb,
 role text NOT NULL CHECK(role IN ('user','assistant')),
 status text NOT NULL CHECK(status IN ('completed','partial','cancelled','failed')),
 content text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(tenant_id,conversation_id,client_message_id),
 FOREIGN KEY(tenant_id,conversation_id,topic_id) REFERENCES app_state.topics(tenant_id,conversation_id,id),
 FOREIGN KEY(tenant_id,topic_id,run_id) REFERENCES app_state.runs(tenant_id,topic_id,id));
CREATE TABLE IF NOT EXISTS app_state.query_tickets(
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL, run_id uuid NOT NULL, task_revision bigint NOT NULL,
 auth_version bigint NOT NULL, dataset_version uuid NOT NULL, catalog_version text NOT NULL,
 policy_version text NOT NULL, sql text NOT NULL, params jsonb NOT NULL, sql_hash text NOT NULL,
 requires_approval bool NOT NULL, explain jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(tenant_id,run_id,id), FOREIGN KEY(tenant_id,run_id) REFERENCES app_state.runs(tenant_id,id));
CREATE TABLE IF NOT EXISTS app_state.query_evidence(
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL, run_id uuid NOT NULL, task_revision bigint NOT NULL,
 query_id uuid NOT NULL, content jsonb NOT NULL, sha256 text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(tenant_id,run_id,id),
 FOREIGN KEY(tenant_id,run_id,query_id) REFERENCES app_state.query_tickets(tenant_id,run_id,id));
CREATE TABLE IF NOT EXISTS app_state.confirmations(
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL, run_id uuid NOT NULL, user_id uuid NOT NULL,
 task_revision bigint NOT NULL, auth_version bigint NOT NULL, query_id uuid NOT NULL,
 sql_hash text NOT NULL, checkpoint_digest text NOT NULL, budget jsonb NOT NULL,
 token_hash text NOT NULL UNIQUE, expires_at timestamptz NOT NULL, consumed_at timestamptz,
 FOREIGN KEY(tenant_id,run_id,query_id) REFERENCES app_state.query_tickets(tenant_id,run_id,id),
 FOREIGN KEY(tenant_id,user_id) REFERENCES app_state.users(tenant_id,id));
CREATE TABLE IF NOT EXISTS app_state.events(
 id bigserial PRIMARY KEY, tenant_id uuid NOT NULL, run_id uuid NOT NULL, topic_id uuid NOT NULL,
 epoch bigint NOT NULL, task_revision bigint NOT NULL, kind text NOT NULL, payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(tenant_id,topic_id,run_id) REFERENCES app_state.runs(tenant_id,topic_id,id));
CREATE TABLE IF NOT EXISTS app_state.audits(
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL, user_id uuid NOT NULL,
 action text NOT NULL, object_id uuid, details jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(tenant_id,user_id) REFERENCES app_state.users(tenant_id,id));
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA app_state TO ip_state;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA app_state TO ip_state;
ALTER DEFAULT PRIVILEGES IN SCHEMA graph_checkpoint GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO ip_checkpoint;
RESET ROLE;
