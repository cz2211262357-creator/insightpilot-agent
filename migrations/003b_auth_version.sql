SET ROLE ip_owner;
CREATE OR REPLACE FUNCTION app_state.bump_auth_version() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,app_state AS $$
BEGIN
 IF NEW.auth_version<OLD.auth_version THEN RAISE EXCEPTION 'auth_version_must_not_regress' USING ERRCODE='23514'; END IF;
 IF (NEW.enabled,NEW.role,NEW.password_hash) IS DISTINCT FROM (OLD.enabled,OLD.role,OLD.password_hash) THEN
  NEW.auth_version:=greatest(NEW.auth_version,OLD.auth_version+1);
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION app_state.bump_auth_version() FROM PUBLIC;
DROP TRIGGER IF EXISTS authorization_version ON app_state.users;
CREATE TRIGGER authorization_version BEFORE UPDATE ON app_state.users FOR EACH ROW EXECUTE FUNCTION app_state.bump_auth_version();
RESET ROLE;
