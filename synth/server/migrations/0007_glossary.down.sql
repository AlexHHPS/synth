-- Rollback of schema v7 (glossary_terms). Run inside a transaction with the migration lock:
--   BEGIN; SELECT pg_advisory_xact_lock(18280001); \i 0007_glossary.down.sql; COMMIT;
-- Corrected transcript versions stay valid: raw ASR output is never modified.
DROP TABLE IF EXISTS glossary_terms;
DELETE FROM schema_versions WHERE version = 7;
