-- Execute as the project operator, after creating the private backend schema.
-- The backend receives only a membership check, no access to the Auth tables.
CREATE OR REPLACE FUNCTION synth_voice.has_auth_session(session_id uuid, subject_id uuid)
RETURNS boolean LANGUAGE sql SECURITY DEFINER
SET search_path=pg_catalog AS $$
    SELECT EXISTS (SELECT 1 FROM auth.sessions WHERE id=session_id AND user_id=subject_id);
$$;
REVOKE ALL ON FUNCTION synth_voice.has_auth_session(uuid,uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION synth_voice.has_auth_session(uuid,uuid) TO synth_voice_backend;
