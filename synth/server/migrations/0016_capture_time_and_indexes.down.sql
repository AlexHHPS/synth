-- Rollback of schema v16. Run inside a transaction with the migration lock:
--   BEGIN; SELECT pg_advisory_xact_lock(18280001); \i 0016_capture_time_and_indexes.down.sql; COMMIT;
DROP INDEX IF EXISTS folder_memberships_principal;
DROP INDEX IF EXISTS jobs_meeting;
DROP INDEX IF EXISTS meetings_calendar_event;
DROP INDEX IF EXISTS meetings_owner;
DROP INDEX IF EXISTS meetings_recent;
DROP INDEX IF EXISTS meetings_folder;
ALTER TABLE meetings DROP COLUMN IF EXISTS captured_at;
DELETE FROM schema_versions WHERE version = 16;
