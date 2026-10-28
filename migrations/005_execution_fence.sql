SET ROLE ip_owner;
ALTER TABLE app_state.runs ADD COLUMN IF NOT EXISTS waiting_until timestamptz;
ALTER TABLE app_state.runs ADD COLUMN IF NOT EXISTS resume_requested bool NOT NULL DEFAULT false;
CREATE OR REPLACE FUNCTION app_state.lock_execution(
 p_tenant uuid,p_run uuid,p_topic uuid,p_revision bigint,p_execution bigint,p_owner uuid,p_auth bigint)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,app_state AS $$
DECLARE r app_state.runs%ROWTYPE; u app_state.users%ROWTYPE; c app_state.conversations%ROWTYPE;
BEGIN
 SELECT * INTO r FROM app_state.runs WHERE tenant_id=p_tenant AND id=p_run AND topic_id=p_topic;
 IF NOT FOUND THEN RETURN false; END IF;
 SELECT * INTO u FROM app_state.users WHERE tenant_id=p_tenant AND id=r.user_id FOR SHARE;
 IF NOT FOUND OR NOT u.enabled OR u.auth_version<>p_auth OR r.auth_version<>p_auth THEN RETURN false; END IF;
 SELECT * INTO c FROM app_state.conversations WHERE tenant_id=p_tenant AND id=r.conversation_id FOR SHARE;
 SELECT * INTO r FROM app_state.runs WHERE tenant_id=p_tenant AND id=p_run AND topic_id=p_topic FOR UPDATE;
 IF NOT FOUND OR r.state<>'RUNNING' OR r.task_revision<>p_revision OR r.execution_version<>p_execution
    OR r.owner IS DISTINCT FROM p_owner OR r.lease_until IS NULL OR r.lease_until<=clock_timestamp()
    OR r.active_deadline IS NULL OR r.active_deadline<=clock_timestamp() THEN RETURN false; END IF;
 IF r.scope='INTERACTIVE' AND (r.epoch<>c.epoch OR r.topic_id IS DISTINCT FROM c.active_topic_id) THEN RETURN false; END IF;
 RETURN true;
END $$;
REVOKE ALL ON FUNCTION app_state.lock_execution(uuid,uuid,uuid,bigint,bigint,uuid,bigint) FROM PUBLIC;
GRANT USAGE ON SCHEMA app_state TO ip_checkpoint;
GRANT EXECUTE ON FUNCTION app_state.lock_execution(uuid,uuid,uuid,bigint,bigint,uuid,bigint) TO ip_state,ip_checkpoint;
RESET ROLE;
