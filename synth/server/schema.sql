-- Private library. Apply under a PostgreSQL transaction and advisory migration lock.
CREATE TABLE IF NOT EXISTS schema_versions (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS principals (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('user', 'machine')),
    revoked_at timestamptz
);
CREATE TABLE IF NOT EXISTS access_keys (
    id uuid PRIMARY KEY,
    principal_id uuid NOT NULL REFERENCES principals(id),
    token_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);
CREATE TABLE IF NOT EXISTS folders (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES principals(id),
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS folder_memberships (
    folder_id uuid NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    principal_id uuid NOT NULL REFERENCES principals(id),
    role text NOT NULL CHECK (role IN ('reader', 'editor')),
    PRIMARY KEY (folder_id, principal_id)
);
-- Machine keys are independently scoped; principal membership cannot widen a key.
CREATE TABLE IF NOT EXISTS key_folder_scopes (
    key_id uuid NOT NULL REFERENCES access_keys(id) ON DELETE CASCADE,
    folder_id uuid NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    PRIMARY KEY (key_id, folder_id)
);
CREATE TABLE IF NOT EXISTS meetings (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES principals(id),
    folder_id uuid REFERENCES folders(id),
    title text NOT NULL,
    state text NOT NULL CHECK (state IN ('capturing', 'queued', 'processing', 'ready', 'failed', 'cancelled')),
    notes text NOT NULL DEFAULT '',
    revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS transcript_versions (
    meeting_id uuid NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    version integer NOT NULL,
    content jsonb NOT NULL,
    content_hash text NOT NULL,
    model_fingerprint text NOT NULL,
    search_text text NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('spanish', search_text)) STORED,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (meeting_id, version)
);
CREATE INDEX IF NOT EXISTS transcript_search ON transcript_versions USING gin(search_vector);
CREATE TABLE IF NOT EXISTS document_versions (
    meeting_id uuid NOT NULL,
    version integer NOT NULL,
    transcript_version integer NOT NULL,
    content jsonb NOT NULL,
    markdown text NOT NULL,
    content_hash text NOT NULL,
    model_fingerprint text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (meeting_id, version),
    FOREIGN KEY (meeting_id, transcript_version) REFERENCES transcript_versions(meeting_id, version)
);
CREATE TABLE IF NOT EXISTS jobs (
    id uuid PRIMARY KEY,
    meeting_id uuid NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    kind text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    lease_owner text,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    checkpoint jsonb NOT NULL DEFAULT '{}',
    error_code text,
    available_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(state, available_at);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_id uuid REFERENCES principals(id),
    action text NOT NULL,
    resource_id uuid,
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_versions(version) VALUES (1) ON CONFLICT DO NOTHING;
-- Stable capture receipts survive retries and later edits to title/folder.
CREATE TABLE IF NOT EXISTS meeting_creation_receipts (
    owner_id uuid NOT NULL REFERENCES principals(id),
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    meeting_id uuid NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, idempotency_key)
);
INSERT INTO schema_versions(version) VALUES (2) ON CONFLICT DO NOTHING;
ALTER TABLE access_keys ADD COLUMN IF NOT EXISTS issued_by uuid REFERENCES principals(id);
-- Recover provenance only from the already committed key.create audit entry.
UPDATE access_keys k SET issued_by=e.actor_id FROM audit_events e
WHERE k.issued_by IS NULL AND e.action='key.create' AND e.resource_id=k.id AND e.actor_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS access_keys_issuer ON access_keys(issued_by);
INSERT INTO schema_versions(version) VALUES (3) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS auth_identities (
    provider text NOT NULL CHECK (provider='supabase'),
    subject uuid NOT NULL,
    principal_id uuid NOT NULL UNIQUE REFERENCES principals(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider,subject)
);
INSERT INTO schema_versions(version) VALUES (4) ON CONFLICT DO NOTHING;
-- Only encrypted, dimension-validated speaker centroids. Never recordings.
CREATE TABLE IF NOT EXISTS voice_profiles (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES principals(id),
    model text NOT NULL,
    nonce bytea NOT NULL CHECK (octet_length(nonce)=12),
    ciphertext bytea NOT NULL CHECK (octet_length(ciphertext) BETWEEN 256 AND 16384),
    consent_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE voice_profiles ENABLE ROW LEVEL SECURITY;
INSERT INTO schema_versions(version) VALUES (5) ON CONFLICT DO NOTHING;
