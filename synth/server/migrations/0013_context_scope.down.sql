-- Rollback of schema v13 (drafts bound to the folder that supplied their context).
DROP FUNCTION IF EXISTS document_context_allowed(uuid);
ALTER TABLE document_generations DROP COLUMN IF EXISTS context_scope;
DELETE FROM schema_versions WHERE version = 13;
