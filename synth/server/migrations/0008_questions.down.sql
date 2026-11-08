-- Rollback of schema v8 (questions). Run inside a transaction with the migration lock:
--   BEGIN; SELECT pg_advisory_xact_lock(18280001); \i 0008_questions.down.sql; COMMIT;
-- Questions are short-lived (7 days) and hold no source of truth: dropping them loses only pending answers.
DROP TABLE IF EXISTS questions;
DELETE FROM schema_versions WHERE version = 8;
