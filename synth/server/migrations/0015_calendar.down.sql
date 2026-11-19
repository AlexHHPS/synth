-- Rollback of schema v15 (Google Calendar agenda). Meetings keep their content; only the link to an event is removed.
DROP FUNCTION IF EXISTS calendar_context_for_meeting(uuid);
ALTER TABLE meetings DROP COLUMN IF EXISTS calendar_event_id;
DROP TABLE IF EXISTS calendar_events;
DROP TABLE IF EXISTS calendar_connections;
DELETE FROM schema_versions WHERE version = 15;
