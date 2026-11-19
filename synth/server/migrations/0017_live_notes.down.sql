-- Rollback of schema v17. Run inside a transaction with the migration lock:
--   BEGIN; SELECT pg_advisory_xact_lock(18280001); \i 0017_live_notes.down.sql; COMMIT;
-- Provisional notes are disposable; questions to the meetings are kept.
DELETE FROM questions WHERE kind = 'live';
ALTER TABLE questions DROP CONSTRAINT IF EXISTS questions_kind_check;
ALTER TABLE questions DROP COLUMN IF EXISTS kind;
DELETE FROM schema_versions WHERE version = 17;
