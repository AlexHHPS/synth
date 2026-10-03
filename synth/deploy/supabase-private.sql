-- Execute with management credentials in your dedicated project before migrations.
-- ALLOWED_DOMAINS is synchronized by the migration command, not the runtime role.
CREATE SCHEMA IF NOT EXISTS synth_voice;
SET search_path = synth_voice, pg_temp;
CREATE TABLE IF NOT EXISTS allowed_auth_domains (domain text PRIMARY KEY);
REVOKE ALL ON allowed_auth_domains FROM PUBLIC, anon, authenticated;

CREATE OR REPLACE FUNCTION before_user_created(event jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = synth_voice, pg_temp AS $$
DECLARE email text := event->'user'->>'email';
BEGIN
    IF email IS NULL OR email !~ '^[^[:space:]@]+@[^[:space:]@]+$'
       OR NOT EXISTS (SELECT 1 FROM allowed_auth_domains
                      WHERE domain=lower(split_part(email,'@',2))) THEN
        RETURN jsonb_build_object('error',jsonb_build_object(
            'http_code',403,'message','Esta cuenta no pertenece a un dominio autorizado.'));
    END IF;
    RETURN '{}'::jsonb;
END;
$$;
REVOKE ALL ON FUNCTION before_user_created(jsonb) FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA synth_voice TO supabase_auth_admin;
GRANT EXECUTE ON FUNCTION before_user_created(jsonb) TO supabase_auth_admin;

-- Browser/Auth roles do not access these tables. Only our backend applies folder ACLs.
REVOKE ALL ON ALL TABLES IN SCHEMA synth_voice FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA synth_voice FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA synth_voice REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA synth_voice REVOKE ALL ON SEQUENCES FROM PUBLIC, anon, authenticated;
DO $$ DECLARE t record; BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname='synth_voice' LOOP
        EXECUTE format('ALTER TABLE synth_voice.%I ENABLE ROW LEVEL SECURITY',t.tablename);
    END LOOP;
END $$;
