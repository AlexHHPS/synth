-- Apply after migrations as the project management role.
-- Create the dedicated synth_voice_backend LOGIN role separately with a secret password.
GRANT USAGE ON SCHEMA synth_voice TO synth_voice_backend;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA synth_voice TO synth_voice_backend;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA synth_voice TO synth_voice_backend;
DO $$ DECLARE t record; BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname='synth_voice' LOOP
        EXECUTE format('ALTER TABLE synth_voice.%I ENABLE ROW LEVEL SECURITY', t.tablename);
        EXECUTE format('DROP POLICY IF EXISTS synth_backend_access ON synth_voice.%I', t.tablename);
        EXECUTE format('CREATE POLICY synth_backend_access ON synth_voice.%I TO synth_voice_backend USING (true) WITH CHECK (true)', t.tablename);
    END LOOP;
END $$;
-- Reapply after future migrations to include newly added tables.
