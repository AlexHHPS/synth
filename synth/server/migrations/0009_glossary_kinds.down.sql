-- Rollback of schema v9 (glossary taxonomy). Run inside a transaction with the migration lock:
--   BEGIN; SELECT pg_advisory_xact_lock(18280001); \i 0009_glossary_kinds.down.sql; COMMIT;
-- Lossy by design: «producto» and «concepto» have no v7 equivalent and become «otro».
ALTER TABLE glossary_terms DROP CONSTRAINT IF EXISTS glossary_terms_kind_check;
UPDATE glossary_terms SET kind = CASE kind WHEN 'organizacion' THEN 'empresa' WHEN 'producto' THEN 'otro'
    WHEN 'concepto' THEN 'otro' ELSE kind END;
ALTER TABLE glossary_terms ALTER COLUMN kind SET DEFAULT 'otro';
ALTER TABLE glossary_terms ADD CONSTRAINT glossary_terms_kind_check
    CHECK (kind IN ('empresa', 'persona', 'proyecto', 'sistema', 'sigla', 'otro'));
DELETE FROM schema_versions WHERE version = 9;
