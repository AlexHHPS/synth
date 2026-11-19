-- Rollback of schema v14 (voice evidence contributed from meetings). Enrolled profiles are preserved.
DROP FUNCTION IF EXISTS voice_sample_capacity(uuid);
DROP TABLE IF EXISTS voice_profile_samples;
DROP INDEX IF EXISTS voice_profiles_subject_binding;
DELETE FROM schema_versions WHERE version = 14;
