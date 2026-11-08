-- Rollback of schema v6 (tasks, decisions). Run inside a transaction with the migration lock:
--   BEGIN; SELECT pg_advisory_xact_lock(18280001); \i 0006_tasks_decisions.down.sql; COMMIT;
-- Data loss: only rows of these two new tables; minutes keep their copy in document_versions.content.
DROP TABLE IF EXISTS decisions;
DROP TABLE IF EXISTS tasks;
DELETE FROM schema_versions WHERE version = 6;
