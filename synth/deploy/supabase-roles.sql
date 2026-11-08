-- Execute once as the project operator, before the services use these roles (docs/synth/security-rls.md).
-- Runtime roles for v10 (docs/synth/security-rls.md): they log in, own nothing, never bypass RLS and cannot create
-- objects; schema.sql grants them their tables and policies. Passwords are set separately and never stored in the repo.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'synth_voice_api') THEN
        CREATE ROLE synth_voice_api LOGIN NOINHERIT NOBYPASSRLS NOCREATEDB NOCREATEROLE CONNECTION LIMIT 40;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'synth_voice_worker') THEN
        CREATE ROLE synth_voice_worker LOGIN NOINHERIT NOBYPASSRLS NOCREATEDB NOCREATEROLE CONNECTION LIMIT 20;
    END IF;
END $$;
-- Owned by the operator (deploy/supabase-session.sql): only the operator can share it.
GRANT EXECUTE ON FUNCTION synth_voice.has_auth_session(uuid, uuid) TO synth_voice_api;
REVOKE CREATE ON SCHEMA synth_voice FROM synth_voice_api, synth_voice_worker;
