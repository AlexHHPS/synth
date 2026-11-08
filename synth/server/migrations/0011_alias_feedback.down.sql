-- Rollback of schema v11 (alias feedback and correction reviews). Run inside a transaction with the migration lock:
--   BEGIN; SELECT pg_advisory_xact_lock(18280001); \i 0011_alias_feedback.down.sql; COMMIT;
DROP TABLE IF EXISTS correction_reviews;
ALTER TABLE glossary_terms DROP COLUMN IF EXISTS alias_feedback;
DELETE FROM schema_versions WHERE version = 11;
