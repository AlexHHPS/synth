-- Roll back only the 0.5.0 draft/event cache; published documents are preserved.
DROP TABLE IF EXISTS document_generation_events;
DROP TABLE IF EXISTS document_model_responses;
DROP TABLE IF EXISTS document_generations;
DELETE FROM schema_versions WHERE version=12;
