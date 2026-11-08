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
-- v6: tasks and decisions extracted from minutes, editable by people.
CREATE TABLE IF NOT EXISTS tasks (
    id uuid PRIMARY KEY,
    meeting_id uuid NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    document_version integer NOT NULL,
    item_key text NOT NULL,
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
    description text NOT NULL DEFAULT '',
    owner_name text,
    owner_principal_id uuid REFERENCES principals(id) ON DELETE SET NULL,
    owner_evidence text NOT NULL DEFAULT 'ninguno' CHECK (owner_evidence IN ('explicito', 'autocompromiso', 'inferido', 'ninguno')),
    due_text text,
    due_date date,
    status text NOT NULL DEFAULT 'abierta' CHECK (status IN ('abierta', 'hecha', 'descartada')),
    lifecycle text NOT NULL DEFAULT 'nueva' CHECK (lifecycle IN ('nueva', 'continuacion', 'completada')),
    previous_task_id uuid REFERENCES tasks(id) ON DELETE SET NULL,
    citations jsonb NOT NULL DEFAULT '[]',
    confidence real,
    edited_by uuid REFERENCES principals(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (meeting_id, document_version, item_key)
);
CREATE INDEX IF NOT EXISTS tasks_meeting ON tasks(meeting_id, document_version);
CREATE INDEX IF NOT EXISTS tasks_owner ON tasks(owner_principal_id, status);
CREATE TABLE IF NOT EXISTS decisions (
    id uuid PRIMARY KEY,
    meeting_id uuid NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    document_version integer NOT NULL,
    item_key text NOT NULL,
    decision text NOT NULL CHECK (length(decision) BETWEEN 1 AND 1000),
    reason text,
    alternatives jsonb NOT NULL DEFAULT '[]',
    status text NOT NULL DEFAULT 'acordada' CHECK (status IN ('acordada', 'pendiente de validar', 'en desacuerdo', 'aparcada')),
    affects text,
    decided_by text,
    supersedes_id uuid REFERENCES decisions(id) ON DELETE SET NULL,
    citations jsonb NOT NULL DEFAULT '[]',
    edited_by uuid REFERENCES principals(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (meeting_id, document_version, item_key)
);
CREATE INDEX IF NOT EXISTS decisions_meeting ON decisions(meeting_id, document_version);
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE decisions ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN REVOKE ALL ON tasks, decisions FROM anon; END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN REVOKE ALL ON tasks, decisions FROM authenticated; END IF;
END $$;
INSERT INTO schema_versions(version) VALUES (6) ON CONFLICT DO NOTHING;
-- v7: company glossary with «sounds like» aliases; proposed terms need approval.
CREATE TABLE IF NOT EXISTS glossary_terms (
    id uuid PRIMARY KEY,
    scope text NOT NULL CHECK (scope IN ('workspace', 'folder')),
    folder_id uuid REFERENCES folders(id) ON DELETE CASCADE,
    term text NOT NULL CHECK (length(term) BETWEEN 1 AND 120),
    kind text NOT NULL DEFAULT 'otro' CHECK (kind IN ('empresa', 'persona', 'proyecto', 'sistema', 'sigla', 'otro')),
    aliases text[] NOT NULL DEFAULT '{}',
    definition text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'propuesto' CHECK (status IN ('propuesto', 'aprobado', 'descartado')),
    origin text NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'correccion', 'llm', 'fonetico', 'participante', 'carpeta')),
    occurrences integer NOT NULL DEFAULT 0,
    examples jsonb NOT NULL DEFAULT '[]',
    created_by uuid REFERENCES principals(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((scope = 'folder') = (folder_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS glossary_terms_unique
    ON glossary_terms (scope, COALESCE(folder_id, '00000000-0000-0000-0000-000000000000'::uuid), lower(term));
CREATE INDEX IF NOT EXISTS glossary_terms_status ON glossary_terms(status);
ALTER TABLE glossary_terms ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN REVOKE ALL ON glossary_terms FROM anon; END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN REVOKE ALL ON glossary_terms FROM authenticated; END IF;
END $$;
INSERT INTO schema_versions(version) VALUES (7) ON CONFLICT DO NOTHING;
-- v8: questions to the meetings. Passages are retrieved under the asker's permissions at ask time;
-- only the asker reads the row, and rows expire after 7 days (the worker purges them).
CREATE TABLE IF NOT EXISTS questions (
    id uuid PRIMARY KEY,
    asker_id uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    folder_id uuid REFERENCES folders(id) ON DELETE CASCADE,
    meeting_id uuid REFERENCES meetings(id) ON DELETE CASCADE,
    question text NOT NULL CHECK (length(question) BETWEEN 1 AND 500),
    passages jsonb NOT NULL DEFAULT '[]',
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'answered', 'failed')),
    answer jsonb,
    error_code text,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS questions_ready ON questions(state, created_at);
CREATE INDEX IF NOT EXISTS questions_asker ON questions(asker_id, created_at DESC);
ALTER TABLE questions ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN REVOKE ALL ON questions FROM anon; END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN REVOKE ALL ON questions FROM authenticated; END IF;
END $$;
INSERT INTO schema_versions(version) VALUES (8) ON CONFLICT DO NOTHING;
-- v9 : glossary kinds become a taxonomy of what each term is.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_versions WHERE version = 9) THEN
        ALTER TABLE glossary_terms DROP CONSTRAINT IF EXISTS glossary_terms_kind_check;
        UPDATE glossary_terms SET kind = CASE kind WHEN 'empresa' THEN 'organizacion' WHEN 'sigla' THEN 'concepto'
            WHEN 'otro' THEN 'concepto' ELSE kind END;
        ALTER TABLE glossary_terms ALTER COLUMN kind SET DEFAULT 'concepto';
        ALTER TABLE glossary_terms ADD CONSTRAINT glossary_terms_kind_check
            CHECK (kind IN ('persona', 'organizacion', 'producto', 'proyecto', 'sistema', 'concepto'));
    END IF;
END $$;
INSERT INTO schema_versions(version) VALUES (9) ON CONFLICT DO NOTHING;
-- v10: row level security per actor (docs/synth/security-rls.md). The owner role only migrates; the API and the
-- worker connect as their own roles, which are not owners, so these policies apply to them. The API names the
-- authenticated principal in `synth.actor` (and the integration key in `synth.key_id`); the helpers below repeat
-- authorize_folder/authorize_meeting and always re-read the principal and the key, so a revoked one sees nothing.
DO $v10$ DECLARE s text := current_schema(); BEGIN
    EXECUTE format($f$CREATE OR REPLACE FUNCTION rls_actor() RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = %I, pg_temp AS $b$
        SELECT p.id FROM principals p WHERE p.revoked_at IS NULL
            AND p.id = nullif(current_setting('synth.actor', true), '')::uuid
            AND (p.kind = 'user' OR EXISTS (SELECT 1 FROM access_keys k WHERE k.principal_id = p.id AND k.revoked_at IS NULL
                AND k.id = nullif(current_setting('synth.key_id', true), '')::uuid
                AND (k.issued_by IS NULL OR EXISTS (SELECT 1 FROM principals i WHERE i.id = k.issued_by AND i.revoked_at IS NULL)))) $b$$f$, s);
    EXECUTE format($f$CREATE OR REPLACE FUNCTION rls_user() RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = %I, pg_temp AS $b$
        SELECT p.id FROM principals p WHERE p.id = rls_actor() AND p.kind = 'user' $b$$f$, s);
    EXECUTE format($f$CREATE OR REPLACE FUNCTION rls_folder(target uuid, write boolean) RETURNS boolean LANGUAGE sql STABLE
        SECURITY DEFINER SET search_path = %I, pg_temp AS $b$
        SELECT EXISTS (SELECT 1 FROM folders f LEFT JOIN folder_memberships m ON m.folder_id = f.id AND m.principal_id = rls_actor()
            WHERE f.id = target AND rls_actor() IS NOT NULL
              AND (f.owner_id = rls_actor() OR m.role = 'editor' OR (m.role = 'reader' AND NOT write))
              AND (rls_user() IS NOT NULL OR (NOT write AND EXISTS (SELECT 1 FROM key_folder_scopes k
                  WHERE k.folder_id = f.id AND k.key_id = nullif(current_setting('synth.key_id', true), '')::uuid)))) $b$$f$, s);
    EXECUTE format($f$CREATE OR REPLACE FUNCTION rls_meeting(target uuid, write boolean) RETURNS boolean LANGUAGE sql STABLE
        SECURITY DEFINER SET search_path = %I, pg_temp AS $b$
        SELECT EXISTS (SELECT 1 FROM meetings m WHERE m.id = target
            AND ((m.owner_id = rls_user()) OR (m.folder_id IS NOT NULL AND rls_folder(m.folder_id, write)))) $b$$f$, s);
    -- Deleting a folder returns every meeting in it to its owner's private library, members' meetings included:
    -- the one write on rows the actor does not own, so it is a function the folder owner alone can run.
    EXECUTE format($f$CREATE OR REPLACE FUNCTION release_folder(target uuid) RETURNS integer LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER SET search_path = %I, pg_temp AS $b$
        DECLARE released integer;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM folders WHERE id = target AND owner_id = rls_user()) THEN
                RAISE EXCEPTION 'folder_not_owned' USING ERRCODE = '42501';
            END IF;
            UPDATE meetings SET folder_id = NULL, revision = revision + 1, updated_at = now() WHERE folder_id = target;
            GET DIAGNOSTICS released = ROW_COUNT;
            RETURN released;
        END $b$$f$, s);
    REVOKE ALL ON FUNCTION rls_actor(), rls_user(), rls_folder(uuid, boolean), rls_meeting(uuid, boolean), release_folder(uuid) FROM PUBLIC;
    -- Every table, not only the newer ones: a table without RLS would be fully readable by the runtime roles.
    DECLARE t text; BEGIN
        FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = s LOOP
            EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', s, t);
        END LOOP;
    END;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'synth_voice_api') THEN
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO synth_voice_api', s);
        GRANT EXECUTE ON FUNCTION rls_actor(), rls_user(), rls_folder(uuid, boolean), rls_meeting(uuid, boolean),
            release_folder(uuid) TO synth_voice_api;
        -- Identity tables are read before the actor is known (token hash, Supabase subject); they hold no meeting content.
        GRANT SELECT, INSERT, UPDATE ON principals, access_keys, auth_identities, key_folder_scopes TO synth_voice_api;
        GRANT DELETE ON key_folder_scopes TO synth_voice_api;
        GRANT SELECT ON schema_versions TO synth_voice_api;
        GRANT INSERT ON audit_events TO synth_voice_api;
        -- What each endpoint does and no more (an API never deletes a meeting, writes minutes or edits a transcript).
        GRANT SELECT, INSERT, DELETE ON folders, folder_memberships TO synth_voice_api;
        GRANT UPDATE (name) ON folders TO synth_voice_api;
        GRANT UPDATE (role) ON folder_memberships TO synth_voice_api;
        GRANT SELECT, INSERT, UPDATE ON meetings, jobs, glossary_terms, meeting_creation_receipts TO synth_voice_api;
        GRANT SELECT, INSERT ON transcript_versions, questions TO synth_voice_api;
        GRANT SELECT ON document_versions TO synth_voice_api;
        GRANT SELECT, UPDATE ON tasks, decisions TO synth_voice_api;
        GRANT SELECT, INSERT, UPDATE, DELETE ON voice_profiles TO synth_voice_api;
        EXECUTE format('GRANT USAGE ON ALL SEQUENCES IN SCHEMA %I TO synth_voice_api', s);

        DROP POLICY IF EXISTS api_identity ON principals;
        CREATE POLICY api_identity ON principals TO synth_voice_api USING (true) WITH CHECK (true);
        DROP POLICY IF EXISTS api_identity ON access_keys;
        CREATE POLICY api_identity ON access_keys TO synth_voice_api USING (true) WITH CHECK (true);
        DROP POLICY IF EXISTS api_identity ON auth_identities;
        CREATE POLICY api_identity ON auth_identities TO synth_voice_api USING (true) WITH CHECK (true);
        IF to_regclass('allowed_auth_domains') IS NOT NULL THEN  -- Supabase deployments only (deploy/supabase-private.sql)
            GRANT SELECT ON allowed_auth_domains TO synth_voice_api;
            DROP POLICY IF EXISTS api_identity ON allowed_auth_domains;
            CREATE POLICY api_identity ON allowed_auth_domains FOR SELECT TO synth_voice_api USING (true);
        END IF;
        DROP POLICY IF EXISTS api_identity ON schema_versions;
        CREATE POLICY api_identity ON schema_versions FOR SELECT TO synth_voice_api USING (true);
        DROP POLICY IF EXISTS api_insert ON audit_events;
        CREATE POLICY api_insert ON audit_events FOR INSERT TO synth_voice_api WITH CHECK (true);
        -- A key's scope is read while authorizing; only a folder owner (or the local admin issuing keys) changes it.
        DROP POLICY IF EXISTS api_read ON key_folder_scopes;
        CREATE POLICY api_read ON key_folder_scopes FOR SELECT TO synth_voice_api USING (true);
        DROP POLICY IF EXISTS api_write ON key_folder_scopes;
        CREATE POLICY api_write ON key_folder_scopes FOR ALL TO synth_voice_api
            USING (rls_user() = '00000000-0000-4000-8000-000000000001' OR EXISTS (SELECT 1 FROM folders f WHERE f.id = folder_id AND f.owner_id = rls_user()))
            WITH CHECK (rls_user() = '00000000-0000-4000-8000-000000000001' OR EXISTS (SELECT 1 FROM folders f WHERE f.id = folder_id AND f.owner_id = rls_user()));

        DROP POLICY IF EXISTS api_read ON folders;
        CREATE POLICY api_read ON folders FOR SELECT TO synth_voice_api USING (rls_folder(id, false));
        DROP POLICY IF EXISTS api_insert ON folders;
        CREATE POLICY api_insert ON folders FOR INSERT TO synth_voice_api WITH CHECK (owner_id = rls_user());
        DROP POLICY IF EXISTS api_update ON folders;
        CREATE POLICY api_update ON folders FOR UPDATE TO synth_voice_api
            USING (rls_user() IS NOT NULL AND rls_folder(id, true)) WITH CHECK (rls_user() IS NOT NULL AND rls_folder(id, true));
        DROP POLICY IF EXISTS api_delete ON folders;
        CREATE POLICY api_delete ON folders FOR DELETE TO synth_voice_api USING (owner_id = rls_user());

        DROP POLICY IF EXISTS api_read ON folder_memberships;
        CREATE POLICY api_read ON folder_memberships FOR SELECT TO synth_voice_api
            USING (principal_id = rls_actor() OR rls_folder(folder_id, false));
        DROP POLICY IF EXISTS api_write ON folder_memberships;
        CREATE POLICY api_write ON folder_memberships FOR ALL TO synth_voice_api
            USING (EXISTS (SELECT 1 FROM folders f WHERE f.id = folder_id AND f.owner_id = rls_user()))
            WITH CHECK (EXISTS (SELECT 1 FROM folders f WHERE f.id = folder_id AND f.owner_id = rls_user()));

        DROP POLICY IF EXISTS api_read ON meetings;
        CREATE POLICY api_read ON meetings FOR SELECT TO synth_voice_api
            USING (owner_id = rls_user() OR (folder_id IS NOT NULL AND rls_folder(folder_id, false)));
        DROP POLICY IF EXISTS api_insert ON meetings;
        CREATE POLICY api_insert ON meetings FOR INSERT TO synth_voice_api
            WITH CHECK (owner_id = rls_user() AND (folder_id IS NULL OR rls_folder(folder_id, true)));
        DROP POLICY IF EXISTS api_update ON meetings;
        CREATE POLICY api_update ON meetings FOR UPDATE TO synth_voice_api
            USING (owner_id = rls_user() OR (folder_id IS NOT NULL AND rls_folder(folder_id, true)))
            WITH CHECK (owner_id = rls_user() OR (folder_id IS NOT NULL AND rls_folder(folder_id, true)));
        DROP POLICY IF EXISTS api_delete ON meetings;
        CREATE POLICY api_delete ON meetings FOR DELETE TO synth_voice_api USING (owner_id = rls_user());

        DECLARE t text; BEGIN
            FOREACH t IN ARRAY ARRAY['transcript_versions', 'document_versions', 'jobs', 'tasks', 'decisions'] LOOP
                EXECUTE format('DROP POLICY IF EXISTS api_read ON %I', t);
                EXECUTE format('CREATE POLICY api_read ON %I FOR SELECT TO synth_voice_api USING (rls_meeting(meeting_id, false))', t);
                EXECUTE format('DROP POLICY IF EXISTS api_write ON %I', t);
                EXECUTE format('CREATE POLICY api_write ON %I FOR ALL TO synth_voice_api USING (rls_meeting(meeting_id, true)) WITH CHECK (rls_meeting(meeting_id, true))', t);
            END LOOP;
        END;
        DROP POLICY IF EXISTS api_own ON meeting_creation_receipts;
        CREATE POLICY api_own ON meeting_creation_receipts TO synth_voice_api USING (owner_id = rls_user()) WITH CHECK (owner_id = rls_user());

        DROP POLICY IF EXISTS api_read ON glossary_terms;
        CREATE POLICY api_read ON glossary_terms FOR SELECT TO synth_voice_api
            USING (CASE WHEN scope = 'workspace' THEN rls_user() IS NOT NULL OR (rls_actor() IS NOT NULL AND status = 'aprobado')
                        ELSE rls_folder(folder_id, false) END);
        DROP POLICY IF EXISTS api_write ON glossary_terms;
        CREATE POLICY api_write ON glossary_terms FOR ALL TO synth_voice_api
            USING (CASE WHEN scope = 'workspace' THEN rls_user() IS NOT NULL ELSE rls_user() IS NOT NULL AND rls_folder(folder_id, true) END)
            WITH CHECK (CASE WHEN scope = 'workspace' THEN rls_user() IS NOT NULL ELSE rls_user() IS NOT NULL AND rls_folder(folder_id, true) END);

        DROP POLICY IF EXISTS api_own ON questions;
        CREATE POLICY api_own ON questions TO synth_voice_api USING (asker_id = rls_actor()) WITH CHECK (asker_id = rls_actor());

        -- Shared voice profiles: every employee reads them (to compare on their Mac), only the owner writes; keys never.
        DROP POLICY IF EXISTS api_read ON voice_profiles;
        CREATE POLICY api_read ON voice_profiles FOR SELECT TO synth_voice_api USING (rls_user() IS NOT NULL);
        DROP POLICY IF EXISTS api_write ON voice_profiles;
        CREATE POLICY api_write ON voice_profiles FOR ALL TO synth_voice_api USING (owner_id = rls_user()) WITH CHECK (owner_id = rls_user());
    END IF;

    -- The worker processes everyone's jobs, so its policies do not filter by actor; what bounds it is its grants: meeting
    -- content and the glossary, names of people and folders, and no keys, identities, voice profiles or sharing.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'synth_voice_worker') THEN
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO synth_voice_worker', s);
        GRANT SELECT ON principals, folders, schema_versions TO synth_voice_worker;
        GRANT INSERT ON audit_events TO synth_voice_worker;
        GRANT SELECT, UPDATE ON meetings, jobs TO synth_voice_worker;
        GRANT SELECT, INSERT ON transcript_versions, document_versions TO synth_voice_worker;
        GRANT SELECT, INSERT, UPDATE ON tasks, decisions, glossary_terms TO synth_voice_worker;
        GRANT SELECT, UPDATE, DELETE ON questions TO synth_voice_worker;
        EXECUTE format('GRANT USAGE ON ALL SEQUENCES IN SCHEMA %I TO synth_voice_worker', s);
        DECLARE t text; BEGIN
            FOREACH t IN ARRAY ARRAY['principals', 'folders', 'schema_versions', 'audit_events', 'meetings', 'transcript_versions',
                                     'document_versions', 'jobs', 'tasks', 'decisions', 'glossary_terms', 'questions'] LOOP
                EXECUTE format('DROP POLICY IF EXISTS worker_all ON %I', t);
                EXECUTE format('CREATE POLICY worker_all ON %I TO synth_voice_worker USING (true) WITH CHECK (true)', t);
            END LOOP;
        END;
    END IF;
END $v10$;
-- Workspace terms are read by everyone: drop the transcript excerpts older versions stored on them (see _examples).
UPDATE glossary_terms SET examples = '[]' WHERE scope = 'workspace' AND examples <> '[]';
INSERT INTO schema_versions(version) VALUES (10) ON CONFLICT DO NOTHING;
-- v11: what people answer about alias corrections. alias_feedback counts acceptances («si») and rejections («no»)
-- per alias of a term; a rejected suggestion is kept per meeting here, since transcript versions are immutable and a
-- new version would mean new minutes for an unchanged text.
ALTER TABLE glossary_terms ADD COLUMN IF NOT EXISTS alias_feedback jsonb NOT NULL DEFAULT '{}';
CREATE TABLE IF NOT EXISTS correction_reviews (
    meeting_id uuid NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    correction_id text NOT NULL CHECK (correction_id ~ '^c[0-9]{4}$'),
    decision text NOT NULL CHECK (decision IN ('rechazada')),
    reviewed_by uuid REFERENCES principals(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (meeting_id, correction_id)
);
ALTER TABLE correction_reviews ENABLE ROW LEVEL SECURITY;
DO $v11$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN REVOKE ALL ON correction_reviews FROM anon; END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN REVOKE ALL ON correction_reviews FROM authenticated; END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'synth_voice_api') THEN
        GRANT SELECT, INSERT ON correction_reviews TO synth_voice_api;
        DROP POLICY IF EXISTS api_read ON correction_reviews;
        CREATE POLICY api_read ON correction_reviews FOR SELECT TO synth_voice_api USING (rls_meeting(meeting_id, false));
        DROP POLICY IF EXISTS api_write ON correction_reviews;
        CREATE POLICY api_write ON correction_reviews FOR INSERT TO synth_voice_api WITH CHECK (rls_meeting(meeting_id, true));
    END IF;
END $v11$;
INSERT INTO schema_versions(version) VALUES (11) ON CONFLICT DO NOTHING;

-- v12: durable draft events. The worker owns generation; API clients only read.
CREATE TABLE IF NOT EXISTS document_generations (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    meeting_id uuid NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    transcript_version integer NOT NULL,
    result_transcript_version integer,
    document_version integer,
    attempt integer NOT NULL CHECK (attempt>0),
    state text NOT NULL DEFAULT 'running' CHECK (state IN ('running','complete','failed','superseded')),
    last_sequence bigint NOT NULL DEFAULT 0 CHECK (last_sequence>=0),
    bytes integer NOT NULL DEFAULT 0 CHECK (bytes BETWEEN 0 AND 16777216),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id,attempt),
    UNIQUE (id,meeting_id),
    FOREIGN KEY (meeting_id,transcript_version) REFERENCES transcript_versions(meeting_id,version)
);
CREATE INDEX IF NOT EXISTS document_generations_meeting ON document_generations(meeting_id,created_at DESC);
CREATE TABLE IF NOT EXISTS document_generation_events (
    generation_id uuid NOT NULL,
    meeting_id uuid NOT NULL,
    sequence bigint NOT NULL CHECK (sequence>0),
    event_key text NOT NULL CHECK (length(event_key) BETWEEN 1 AND 200),
    kind text NOT NULL CHECK (kind IN ('started','topic','delta','complete','failed')),
    content jsonb NOT NULL CHECK (octet_length(content::text)<=65536),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (generation_id,sequence),
    UNIQUE (generation_id,event_key),
    FOREIGN KEY (generation_id,meeting_id) REFERENCES document_generations(id,meeting_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS document_model_responses (
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    request_hash text NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
    response jsonb NOT NULL CHECK (octet_length(response::text)<=16777216),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id,request_hash)
);
ALTER TABLE document_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_generation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_model_responses ENABLE ROW LEVEL SECURITY;
DO $v12$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['document_generations','document_generation_events','document_model_responses'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN EXECUTE format('REVOKE ALL ON %I FROM anon',t); END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN EXECUTE format('REVOKE ALL ON %I FROM authenticated',t); END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='synth_voice_worker') THEN
            EXECUTE format('GRANT SELECT,INSERT,UPDATE ON %I TO synth_voice_worker',t);
            EXECUTE format('DROP POLICY IF EXISTS worker_all ON %I',t);
            EXECUTE format('CREATE POLICY worker_all ON %I TO synth_voice_worker USING (true) WITH CHECK (true)',t);
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='synth_voice_api') THEN
        GRANT SELECT ON document_generations,document_generation_events TO synth_voice_api;
        REVOKE ALL ON document_model_responses FROM synth_voice_api;
        DROP POLICY IF EXISTS api_read ON document_generations;
        CREATE POLICY api_read ON document_generations FOR SELECT TO synth_voice_api USING (rls_meeting(meeting_id,false));
        DROP POLICY IF EXISTS api_read ON document_generation_events;
        CREATE POLICY api_read ON document_generation_events FOR SELECT TO synth_voice_api USING (rls_meeting(meeting_id,false));
    END IF;
END $v12$;
INSERT INTO schema_versions(version) VALUES (12) ON CONFLICT DO NOTHING;

-- v13: in-flight drafts remain bound to the folder that supplied their context.
-- Legacy drafts without a receipt stay hidden; published documents are preserved.
ALTER TABLE document_generations ADD COLUMN IF NOT EXISTS context_scope jsonb;
DO $v13$ DECLARE s text := current_schema(); BEGIN
    EXECUTE format($f$CREATE OR REPLACE FUNCTION document_context_allowed(target uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = %I, pg_temp AS $b$
        SELECT EXISTS (SELECT 1 FROM meetings m JOIN principals p ON p.id=m.owner_id
            LEFT JOIN folders f ON f.id=m.folder_id
            LEFT JOIN folder_memberships fm ON fm.folder_id=m.folder_id AND fm.principal_id=m.owner_id
            WHERE m.id=target AND p.kind='user' AND p.revoked_at IS NULL
              AND (m.folder_id IS NULL OR f.owner_id=m.owner_id OR fm.role IN ('reader','editor'))) $b$$f$,s);
    REVOKE ALL ON FUNCTION document_context_allowed(uuid) FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='synth_voice_worker') THEN
        GRANT EXECUTE ON FUNCTION document_context_allowed(uuid) TO synth_voice_worker;
    END IF;
END $v13$;
INSERT INTO schema_versions(version) VALUES (13) ON CONFLICT DO NOTHING;

-- v14: encrypted voice evidence supplements enrollment; only its subject accepts a contribution.
CREATE UNIQUE INDEX IF NOT EXISTS voice_profiles_subject_binding ON voice_profiles(id,owner_id);
CREATE TABLE IF NOT EXISTS voice_profile_samples (
    id uuid PRIMARY KEY,
    root_profile_id uuid NOT NULL,
    owner_id uuid NOT NULL REFERENCES principals(id),
    contributor_id uuid NOT NULL REFERENCES principals(id),
    model text NOT NULL CHECK (length(model) BETWEEN 1 AND 100),
    nonce bytea,
    ciphertext bytea,
    state text NOT NULL CHECK (state IN ('pending','accepted','rejected')),
    evidence jsonb NOT NULL CHECK (octet_length(evidence::text)<=16384),
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(root_profile_id,owner_id) REFERENCES voice_profiles(id,owner_id) ON DELETE CASCADE,
    CHECK ((state='rejected' AND nonce IS NULL AND ciphertext IS NULL) OR
           (state<>'rejected' AND nonce IS NOT NULL AND ciphertext IS NOT NULL AND octet_length(nonce)=12 AND octet_length(ciphertext) BETWEEN 256 AND 16384))
);
CREATE INDEX IF NOT EXISTS voice_samples_subject ON voice_profile_samples(owner_id,state,updated_at DESC);
ALTER TABLE voice_profile_samples ENABLE ROW LEVEL SECURITY;
DO $v14capacity$ DECLARE s text:=current_schema(); BEGIN
    EXECUTE format($f$CREATE OR REPLACE FUNCTION voice_sample_capacity(target uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=%I,pg_temp AS $b$
        SELECT rls_user() IS NOT NULL AND EXISTS (SELECT 1 FROM principals WHERE id=target AND kind='user' AND revoked_at IS NULL)
          AND (SELECT count(*) FROM voice_profile_samples WHERE owner_id=target AND state<>'rejected')<64 $b$$f$,s);
    REVOKE ALL ON FUNCTION voice_sample_capacity(uuid) FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='synth_voice_api') THEN
        GRANT EXECUTE ON FUNCTION voice_sample_capacity(uuid) TO synth_voice_api;
    END IF;
END $v14capacity$;
DO $v14$ BEGIN
    REVOKE ALL ON voice_profile_samples FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN REVOKE ALL ON voice_profile_samples FROM anon; END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN REVOKE ALL ON voice_profile_samples FROM authenticated; END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='synth_voice_worker') THEN REVOKE ALL ON voice_profile_samples FROM synth_voice_worker; END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='synth_voice_api') THEN
        GRANT SELECT,INSERT,UPDATE ON voice_profile_samples TO synth_voice_api;
        DROP POLICY IF EXISTS api_read ON voice_profile_samples;
        CREATE POLICY api_read ON voice_profile_samples FOR SELECT TO synth_voice_api
            USING (rls_user() IS NOT NULL AND (state='accepted' OR owner_id=rls_user() OR contributor_id=rls_user()));
        DROP POLICY IF EXISTS api_insert ON voice_profile_samples;
        CREATE POLICY api_insert ON voice_profile_samples FOR INSERT TO synth_voice_api
            WITH CHECK (contributor_id=rls_user() AND (state='pending' OR (state='accepted' AND owner_id=rls_user())));
        DROP POLICY IF EXISTS api_update ON voice_profile_samples;
        CREATE POLICY api_update ON voice_profile_samples FOR UPDATE TO synth_voice_api
            USING (owner_id=rls_user() OR contributor_id=rls_user())
            WITH CHECK (owner_id=rls_user() OR (contributor_id=rls_user() AND state='rejected'));
    END IF;
END $v14$;
INSERT INTO schema_versions(version) VALUES (14) ON CONFLICT DO NOTHING;

-- Personal calendar grants never inherit folder membership or machine-key access.
CREATE TABLE IF NOT EXISTS calendar_connections (
    owner_id uuid PRIMARY KEY REFERENCES principals(id),
    email text NOT NULL,
    nonce bytea NOT NULL,
    ciphertext bytea NOT NULL,
    revision integer NOT NULL DEFAULT 1,
    synced_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS calendar_events (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES calendar_connections(owner_id) ON DELETE CASCADE,
    provider_id text NOT NULL,
    content jsonb NOT NULL,
    content_hash text NOT NULL,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    folder_id uuid REFERENCES folders(id) ON DELETE SET NULL,
    active boolean NOT NULL DEFAULT true,
    prepared_pack jsonb,
    prepared_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(owner_id,provider_id)
);
CREATE INDEX IF NOT EXISTS calendar_events_agenda ON calendar_events(owner_id,starts_at) WHERE active;
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS calendar_event_id uuid REFERENCES calendar_events(id) ON DELETE SET NULL;
ALTER TABLE calendar_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE calendar_events ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF EXISTS(SELECT FROM pg_roles WHERE rolname='synth_voice_api') THEN
        GRANT SELECT,INSERT,UPDATE,DELETE ON calendar_connections,calendar_events TO synth_voice_api;
        DROP POLICY IF EXISTS api_own ON calendar_connections;
        CREATE POLICY api_own ON calendar_connections TO synth_voice_api
            USING(owner_id=rls_user()) WITH CHECK(owner_id=rls_user());
        DROP POLICY IF EXISTS api_own ON calendar_events;
        CREATE POLICY api_own ON calendar_events TO synth_voice_api
            USING(owner_id=rls_user()) WITH CHECK(owner_id=rls_user());
    END IF;
END $$;
-- Worker can read only the linked, currently authorized event; never provider credentials.
CREATE OR REPLACE FUNCTION calendar_context_for_meeting(target uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path FROM CURRENT AS $$
    SELECT e.content FROM meetings m JOIN calendar_events e ON e.id=m.calendar_event_id
    JOIN calendar_connections c ON c.owner_id=e.owner_id JOIN principals p ON p.id=m.owner_id
    WHERE m.id=target AND e.owner_id=m.owner_id AND p.revoked_at IS NULL AND e.active
      AND e.folder_id IS NOT DISTINCT FROM m.folder_id AND document_context_allowed(m.id)
$$;
REVOKE ALL ON FUNCTION calendar_context_for_meeting(uuid) FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS(SELECT FROM pg_roles WHERE rolname='synth_voice_worker') THEN
        GRANT EXECUTE ON FUNCTION calendar_context_for_meeting(uuid) TO synth_voice_worker;
    END IF;
END $$;
INSERT INTO schema_versions(version) VALUES (15) ON CONFLICT DO NOTHING;
