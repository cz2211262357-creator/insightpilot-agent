ALTER ROLE ip_checkpoint SET search_path=graph_checkpoint,pg_catalog;
SET ROLE ip_owner;
CREATE OR REPLACE FUNCTION app_state.guard_checkpoint(
 p_user uuid,p_role text,p_tenant uuid,p_run uuid,p_topic uuid,p_revision bigint,
 p_execution bigint,p_owner uuid,p_auth bigint,p_thread text,p_namespace text,p_digest text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,app_state AS $$
DECLARE r app_state.runs%ROWTYPE; actual_role text;
BEGIN
 SELECT * INTO r FROM app_state.runs WHERE tenant_id=p_tenant AND id=p_run AND topic_id=p_topic;
 IF NOT FOUND OR r.user_id<>p_user THEN RETURN false; END IF;
 IF NOT app_state.lock_execution(p_tenant,p_run,p_topic,p_revision,p_execution,p_owner,p_auth) THEN RETURN false; END IF;
 SELECT * INTO r FROM app_state.runs WHERE tenant_id=p_tenant AND id=p_run;
 SELECT role INTO actual_role FROM app_state.users WHERE tenant_id=p_tenant AND id=p_user;
 IF actual_role<>p_role OR r.thread_id::text<>p_thread OR p_namespace<>'' THEN RETURN false; END IF;
 IF p_digest IS NOT NULL THEN
  IF p_digest !~ '^[0-9a-f]{64}$' THEN RETURN false; END IF;
  UPDATE app_state.runs SET checkpoint_digest=
    encode(sha256(convert_to(coalesce(checkpoint_digest,'')||':'||p_digest,'UTF8')),'hex')
    WHERE tenant_id=p_tenant AND id=p_run;
 END IF;
 RETURN true;
END $$;
REVOKE ALL ON FUNCTION app_state.guard_checkpoint(uuid,text,uuid,uuid,uuid,bigint,bigint,uuid,bigint,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_state.guard_checkpoint(uuid,text,uuid,uuid,uuid,bigint,bigint,uuid,bigint,text,text,text) TO ip_checkpoint;
RESET ROLE;
