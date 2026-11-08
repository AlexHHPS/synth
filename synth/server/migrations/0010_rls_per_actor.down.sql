-- Rollback of schema v10 (row level security per actor). Run inside a transaction with the migration lock:
--   BEGIN; SELECT pg_advisory_xact_lock(18280001); \i 0010_rls_per_actor.down.sql; COMMIT;
-- RLS stays enabled with no policy for the runtime roles, so point the services back at the owner role first.
DO $$ DECLARE p record; BEGIN
    FOR p IN SELECT policyname, tablename FROM pg_policies WHERE schemaname = current_schema()
             AND policyname IN ('api_identity', 'api_insert', 'api_read', 'api_write', 'api_update', 'api_delete', 'api_own', 'worker_all') LOOP
        EXECUTE format('DROP POLICY %I ON %I', p.policyname, p.tablename);
    END LOOP;
END $$;
DROP FUNCTION IF EXISTS release_folder(uuid);
DROP FUNCTION IF EXISTS rls_meeting(uuid, boolean);
DROP FUNCTION IF EXISTS rls_folder(uuid, boolean);
DROP FUNCTION IF EXISTS rls_user();
DROP FUNCTION IF EXISTS rls_actor();
DELETE FROM schema_versions WHERE version = 10;
