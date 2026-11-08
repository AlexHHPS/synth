"""v10: PostgreSQL itself keeps each employee's library private when the API connects as its own role.

The API role is not the owner, so row policies apply to it: whatever an endpoint forgets, a query only reaches the rows
the bound actor may see (owner, folder membership, integration key scope), and nothing once the actor is revoked.
"""
import os
import unittest
import unittest.mock
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "server/schema.sql"
ADMIN = "00000000-0000-4000-8000-000000000001"


@unittest.skipUnless(os.environ.get("SYNTH_TEST_DATABASE_URL"), "Disposable PostgreSQL URL required")
class RowSecurityTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        url = os.environ["SYNTH_TEST_DATABASE_URL"]
        if "@127.0.0.1:18289/" not in url:
            self.fail("Tests require the dedicated disposable local PostgreSQL port")
        self.url, self.schema = url, "rls_" + uuid4().hex
        self.owner = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        self.owner.execute("""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='synth_voice_api') THEN CREATE ROLE synth_voice_api NOLOGIN; END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='synth_voice_worker') THEN CREATE ROLE synth_voice_worker NOLOGIN; END IF;
            END $$""")
        self.owner.execute("CREATE SCHEMA " + self.schema)
        self.owner.execute("SET search_path=" + self.schema)
        self.owner.execute(SCHEMA_SQL.read_text())
        ids = {name: uuid4() for name in ("alice", "bob", "carol", "robot", "key", "old_key", "shared", "private",
                                          "in_shared", "secret")}
        self.ids = ids
        run = self.owner.execute
        for name in ("alice", "bob", "carol"):
            run("INSERT INTO principals(id,name,kind) VALUES (%s,%s,'user')", (ids[name], name))
        run("INSERT INTO principals(id,name,kind) VALUES (%s,'robot','machine')", (ids["robot"],))
        run("INSERT INTO access_keys(id,principal_id,token_hash,issued_by) VALUES (%s,%s,'h1',%s), (%s,%s,'h2',%s)",
            (ids["key"], ids["robot"], ids["alice"], ids["old_key"], ids["robot"], ids["alice"]))
        run("UPDATE access_keys SET revoked_at=now() WHERE id=%s", (ids["old_key"],))
        run("INSERT INTO folders(id,owner_id,name) VALUES (%s,%s,'Clientes'), (%s,%s,'Privada')",
            (ids["shared"], ids["alice"], ids["private"], ids["alice"]))
        run("INSERT INTO folder_memberships VALUES (%s,%s,'reader'), (%s,%s,'reader')",
            (ids["shared"], ids["bob"], ids["shared"], ids["robot"]))
        run("INSERT INTO key_folder_scopes VALUES (%s,%s)", (ids["key"], ids["shared"]))
        run("""INSERT INTO meetings(id,owner_id,folder_id,title,state) VALUES (%s,%s,%s,'Comité','ready'),
               (%s,%s,NULL,'Uno a uno','ready')""", (ids["in_shared"], ids["alice"], ids["shared"], ids["secret"], ids["alice"]))
        for meeting in ("in_shared", "secret"):
            run("""INSERT INTO transcript_versions(meeting_id,version,content,content_hash,model_fingerprint,search_text)
                   VALUES (%s,1,'{}','h','m','texto')""", (ids[meeting],))
            run("INSERT INTO tasks(id,meeting_id,document_version,item_key,title) VALUES (%s,%s,1,'t1','Enviar propuesta')",
                (uuid4(), ids[meeting]))
        run("INSERT INTO questions(id,asker_id,question) VALUES (%s,%s,'¿Qué se decidió?')", (uuid4(), ids["alice"]))
        run("""INSERT INTO voice_profiles(id,owner_id,model,nonce,ciphertext) VALUES (%s,%s,'m',%s,%s)""",
            (uuid4(), ids["alice"], b"\0" * 12, b"\0" * 256))
        run("""INSERT INTO glossary_terms(id,scope,folder_id,term,status) VALUES (%s,'folder',%s,'Lucerno','aprobado'),
               (%s,'workspace',NULL,'Valdivar','aprobado'), (%s,'workspace',NULL,'Baldibar','propuesto')""",
            (uuid4(), ids["shared"], uuid4(), uuid4()))

    def tearDown(self):
        self.owner.execute("DROP SCHEMA " + self.schema + " CASCADE")
        self.owner.close()

    @contextmanager
    def api(self, actor=None, key=None, role="synth_voice_api"):
        """A connection as the API role with `actor` bound, like synth.server.db.bind_actor does per request."""
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(self.url, autocommit=True, row_factory=dict_row) as db:
            db.execute("SET ROLE " + role)
            db.execute("SET search_path=" + self.schema)
            if actor:
                db.execute("SELECT set_config('synth.actor', %s, false), set_config('synth.key_id', %s, false)",
                           (str(actor), str(key or "")))
            yield db

    def meetings(self, db):
        ids = {v: k for k, v in self.ids.items()}
        return {ids[r["id"]] for r in db.execute("SELECT id FROM meetings").fetchall()}

    def tasks(self, db):
        ids = {v: k for k, v in self.ids.items()}
        return {ids[r["meeting_id"]] for r in db.execute("SELECT meeting_id FROM tasks").fetchall()}

    def test_each_employee_reads_only_their_library_and_the_folders_shared_with_them(self):
        with self.api(self.ids["alice"]) as db:
            self.assertEqual((self.meetings(db), self.tasks(db)), ({"in_shared", "secret"}, {"in_shared", "secret"}))
        with self.api(self.ids["bob"]) as db:
            self.assertEqual((self.meetings(db), self.tasks(db)), ({"in_shared"}, {"in_shared"}))
            self.assertEqual(db.execute("SELECT count(*) AS n FROM transcript_versions").fetchone()["n"], 1)
            self.assertEqual({r["name"] for r in db.execute("SELECT name FROM folders").fetchall()}, {"Clientes"})
        with self.api(self.ids["carol"]) as db:
            self.assertEqual((self.meetings(db), self.tasks(db)), (set(), set()))
            self.assertEqual(db.execute("SELECT count(*) AS n FROM folders").fetchone()["n"], 0)

    def test_calendar_grants_and_events_are_private_and_worker_cannot_read_credentials(self):
        from psycopg.types.json import Jsonb
        import psycopg
        event_id = uuid4()
        self.owner.execute('INSERT INTO calendar_connections(owner_id,email,nonce,ciphertext) VALUES (%s,%s,%s,%s)',
                           (self.ids['alice'], 'alice@example.test', b'n'*12, b'encrypted-token'))
        self.owner.execute('''INSERT INTO calendar_events(id,owner_id,provider_id,content,content_hash,starts_at,ends_at,folder_id)
            VALUES (%s,%s,'event',%s,'hash',now(),now()+interval '1 hour',%s)''',
            (event_id, self.ids['alice'], Jsonb({'title':'Private calendar'}), self.ids['shared']))
        self.owner.execute('UPDATE meetings SET calendar_event_id=%s WHERE id=%s', (event_id, self.ids['in_shared']))
        for name in ('bob','carol','robot'):
            with self.api(self.ids[name], self.ids['key'] if name == 'robot' else None) as db:
                self.assertEqual(db.execute('SELECT * FROM calendar_connections').fetchall(), [])
                self.assertEqual(db.execute('SELECT * FROM calendar_events').fetchall(), [])
        with self.api(self.ids['alice']) as db:
            self.assertEqual(len(db.execute('SELECT * FROM calendar_events').fetchall()), 1)
        with self.api(role='synth_voice_worker') as db:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                db.execute('SELECT ciphertext FROM calendar_connections').fetchall()
            self.assertEqual(db.execute('SELECT calendar_context_for_meeting(%s) AS value',
                             (self.ids['in_shared'],)).fetchone()['value']['title'], 'Private calendar')

    def test_without_a_bound_actor_no_content_is_visible(self):
        with self.api() as db:
            for table in ("meetings", "transcript_versions", "tasks", "folders", "questions", "voice_profiles", "glossary_terms"):
                self.assertEqual(db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"], 0, table)

    def test_checkpoint_context_uses_real_rls_and_never_the_owners_private_history(self):
        import hashlib
        import json
        from psycopg.types.json import Jsonb
        from fastapi import HTTPException
        from synth.server.meeting_context import snapshot
        from synth.tests.test_questions import transcript
        current = uuid4()
        self.owner.execute("INSERT INTO meetings(id,owner_id,folder_id,title,state) VALUES (%s,%s,%s,'Actual','capturing')",
                           (current, self.ids["alice"], self.ids["shared"]))
        content = transcript([("system:S1", "Los permisos se revisan antes de desplegar en Nimbus.")])
        digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        self.owner.execute("UPDATE meetings SET created_at=now()-interval '1 day' WHERE id IN (%s,%s)",
                           (self.ids["in_shared"], self.ids["secret"]))
        self.owner.execute("UPDATE transcript_versions SET content=%s,content_hash=%s,search_text='permisos Nimbus'",
                           (Jsonb(content), digest))
        for who, key in (("alice", None), ("bob", None), ("robot", self.ids["key"])):
            actor = {"id": self.ids[who], "kind": "machine" if who == "robot" else "user", "key_id": key}
            with self.api(actor["id"], key) as db:
                result = snapshot(db, actor, current, "permisos Nimbus")
                self.assertEqual({p["meeting_id"] for p in result["passages"]}, {str(self.ids["in_shared"])})
                self.assertEqual({g["term"] for g in result["glossary"]}, set())
        for who, key in (("carol", None), ("robot", self.ids["old_key"])):
            actor = {"id": self.ids[who], "kind": "machine" if who == "robot" else "user", "key_id": key}
            with self.api(actor["id"], key) as db:
                with self.assertRaises(HTTPException) as denied:
                    snapshot(db, actor, current, "Nimbus")
                self.assertEqual(denied.exception.status_code, 404)

    def test_a_reader_cannot_change_a_shared_meeting(self):
        import psycopg
        with self.api(self.ids["bob"]) as db:
            self.assertEqual(db.execute("UPDATE meetings SET title='x' WHERE id=%s", (self.ids["in_shared"],)).rowcount, 0)
            self.assertEqual(db.execute("UPDATE tasks SET status='hecha' WHERE meeting_id=%s", (self.ids["in_shared"],)).rowcount, 0)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):  # the API never deletes tasks at all
                db.execute("DELETE FROM tasks WHERE meeting_id=%s", (self.ids["in_shared"],))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                db.execute("INSERT INTO tasks(id,meeting_id,document_version,item_key,title) VALUES (%s,%s,1,'t2','x')",
                           (uuid4(), self.ids["in_shared"]))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                db.execute("INSERT INTO meetings(id,owner_id,folder_id,title,state) VALUES (%s,%s,%s,'x','ready')",
                           (uuid4(), self.ids["bob"], self.ids["shared"]))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):  # nobody writes rows in someone else's name
                db.execute("INSERT INTO meetings(id,owner_id,title,state) VALUES (%s,%s,'x','ready')", (uuid4(), self.ids["alice"]))

    def test_only_the_folder_owner_shares_it(self):
        import psycopg
        with self.api(self.ids["bob"]) as db:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                db.execute("INSERT INTO folder_memberships VALUES (%s,%s,'editor')", (self.ids["shared"], self.ids["carol"]))
            self.assertEqual(db.execute("UPDATE folder_memberships SET role='editor' WHERE principal_id=%s",
                                        (self.ids["bob"],)).rowcount, 0)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                db.execute("INSERT INTO key_folder_scopes VALUES (%s,%s)", (self.ids["key"], self.ids["private"]))
        with self.api(self.ids["alice"]) as db:
            db.execute("INSERT INTO folder_memberships VALUES (%s,%s,'editor')", (self.ids["shared"], self.ids["carol"]))
        with self.api(self.ids["carol"]) as db:
            self.assertEqual(db.execute("UPDATE meetings SET title='Comité 2' WHERE id=%s", (self.ids["in_shared"],)).rowcount, 1)
            self.assertEqual(self.meetings(db), {"in_shared"})

    def test_an_editor_renames_a_folder_but_only_its_owner_releases_it(self):
        import psycopg
        self.owner.execute("UPDATE folder_memberships SET role='editor' WHERE principal_id=%s", (self.ids["bob"],))
        self.owner.execute("INSERT INTO meetings(id,owner_id,folder_id,title,state) VALUES (%s,%s,%s,'De Bob','ready')",
                           (uuid4(), self.ids["bob"], self.ids["shared"]))
        with self.api(self.ids["bob"]) as db:
            self.assertEqual(db.execute("UPDATE folders SET name='Clientes 2026' WHERE id=%s", (self.ids["shared"],)).rowcount, 1)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                db.execute("UPDATE folders SET owner_id=%s WHERE id=%s", (self.ids["bob"], self.ids["shared"]))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                db.execute("SELECT release_folder(%s)", (self.ids["shared"],))
        with self.api(self.ids["alice"]) as db:
            self.assertEqual(db.execute("SELECT release_folder(%s) AS n", (self.ids["shared"],)).fetchone()["n"], 2)
            db.execute("DELETE FROM folders WHERE id=%s", (self.ids["shared"],))
        with self.api(self.ids["bob"]) as db:  # Bob's meeting is back in his private library
            self.assertEqual(db.execute("SELECT title FROM meetings").fetchall(), [{"title": "De Bob"}])

    def test_an_integration_key_reads_only_its_scoped_folders_and_only_while_valid(self):
        with self.api(self.ids["robot"], self.ids["key"]) as db:
            self.assertEqual(self.meetings(db), {"in_shared"})
            self.assertEqual(db.execute("UPDATE meetings SET title='x'").rowcount, 0)
            self.assertEqual(db.execute("SELECT count(*) AS n FROM voice_profiles").fetchone()["n"], 0)
            self.assertEqual({r["term"] for r in db.execute("SELECT term FROM glossary_terms").fetchall()}, {"Lucerno", "Valdivar"})
        for key in (self.ids["old_key"], None, uuid4()):
            with self.api(self.ids["robot"], key) as db:
                self.assertEqual(self.meetings(db), set(), key)

    def test_an_integration_key_dies_with_the_employee_who_issued_it(self):
        from synth.server.db import authenticate
        from fastapi import HTTPException
        self.owner.execute("UPDATE access_keys SET token_hash=%s WHERE id=%s", (__import__("synth.server.db", fromlist=["digest"]).digest("synthetic-key"), self.ids["key"]))
        with self.api() as db, unittest.mock.patch.dict(os.environ, {"AUTH_MODE": "keys"}):
            self.assertEqual(authenticate(db, "synthetic-key")["key_id"], self.ids["key"])
        self.owner.execute("UPDATE principals SET revoked_at=now() WHERE id=%s", (self.ids["alice"],))
        with self.api() as db, unittest.mock.patch.dict(os.environ, {"AUTH_MODE": "keys"}):
            with self.assertRaises(HTTPException):
                authenticate(db, "synthetic-key")
        with self.api(self.ids["robot"], self.ids["key"]) as db:
            self.assertEqual(self.meetings(db), set())

    def test_a_revoked_principal_sees_nothing(self):
        self.owner.execute("UPDATE principals SET revoked_at=now() WHERE id=%s", (self.ids["alice"],))
        with self.api(self.ids["alice"]) as db:
            self.assertEqual((self.meetings(db), self.tasks(db)), (set(), set()))

    def test_questions_are_read_by_their_asker_only_and_voice_profiles_written_by_their_owner_only(self):
        with self.api(self.ids["bob"]) as db:
            self.assertEqual(db.execute("SELECT count(*) AS n FROM questions").fetchone()["n"], 0)
            self.assertEqual(db.execute("SELECT count(*) AS n FROM voice_profiles").fetchone()["n"], 1)
            self.assertEqual(db.execute("DELETE FROM voice_profiles").rowcount, 0)
        with self.api(self.ids["alice"]) as db:
            self.assertEqual(db.execute("SELECT count(*) AS n FROM questions").fetchone()["n"], 1)

    def test_the_api_role_cannot_read_the_audit_trail_or_change_the_schema(self):
        import psycopg
        with self.api(self.ids["alice"]) as db:
            db.execute("INSERT INTO audit_events(actor_id,action) VALUES (%s,'test')", (self.ids["alice"],))
            for statement in ("SELECT * FROM audit_events", "CREATE TABLE intrusa(id int)", "DROP TABLE meetings",
                              "ALTER TABLE meetings DISABLE ROW LEVEL SECURITY", "SELECT document_context_allowed(NULL)"):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege, msg=statement):
                    db.execute(statement)

    def test_the_worker_processes_every_meeting_but_never_reaches_keys_identities_voices_or_sharing(self):
        import psycopg
        with self.api(role="synth_voice_worker") as db:
            self.assertEqual(self.meetings(db), {"in_shared", "secret"})
            self.assertEqual(db.execute("UPDATE meetings SET state='ready'").rowcount, 2)
            for table in ("access_keys", "auth_identities", "voice_profiles", "folder_memberships", "key_folder_scopes", "audit_events"):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege, msg=table):
                    db.execute(f"SELECT * FROM {table}")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                db.execute("UPDATE principals SET revoked_at=NULL")

    def test_the_migration_runs_as_a_plain_owner_like_production(self):
        """In Supabase the migrating owner is not a superuser and the operator owns has_auth_session (deploy/)."""
        import psycopg
        schema = "rlsowner_" + uuid4().hex
        self.owner.execute("""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rls_test_owner') THEN CREATE ROLE rls_test_owner NOLOGIN; END IF;
            END $$""")
        self.owner.execute(f"CREATE SCHEMA {schema} AUTHORIZATION rls_test_owner")
        try:
            self.owner.execute(f"""CREATE FUNCTION {schema}.has_auth_session(session_id uuid, subject_id uuid) RETURNS boolean
                LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS 'SELECT false'""")
            self.owner.execute(f"GRANT EXECUTE ON FUNCTION {schema}.has_auth_session(uuid, uuid) TO rls_test_owner")
            with psycopg.connect(self.url, autocommit=True) as db:
                db.execute("SET ROLE rls_test_owner")
                db.execute("SET search_path=" + schema)
                db.execute(SCHEMA_SQL.read_text())
                db.execute(SCHEMA_SQL.read_text())
                self.assertEqual(db.execute("SELECT max(version) FROM schema_versions").fetchone()[0], 15)
        finally:
            self.owner.execute(f"DROP SCHEMA {schema} CASCADE")

    def test_only_who_can_edit_a_meeting_rejects_its_suggestions_and_outsiders_never_see_the_reviews(self):
        import psycopg
        insert = "INSERT INTO correction_reviews(meeting_id,correction_id,decision) VALUES (%s,'c0001','rechazada')"
        with self.api(self.ids["bob"]) as db:  # reader of the shared folder
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                db.execute(insert, (self.ids["in_shared"],))
        with self.api(self.ids["alice"]) as db:
            db.execute(insert, (self.ids["in_shared"],))
        with self.api(self.ids["bob"]) as db:
            self.assertEqual(db.execute("SELECT count(*) AS n FROM correction_reviews").fetchone()["n"], 1)
        with self.api(self.ids["carol"]) as db:
            self.assertEqual(db.execute("SELECT count(*) AS n FROM correction_reviews").fetchone()["n"], 0)

    def test_the_local_admin_can_scope_keys(self):
        self.owner.execute("INSERT INTO principals(id,name,kind) VALUES (%s,'Administrador local','user')", (ADMIN,))
        with self.api(ADMIN) as db:
            db.execute("INSERT INTO key_folder_scopes VALUES (%s,%s)", (self.ids["key"], self.ids["private"]))
        scopes = self.owner.execute("SELECT folder_id FROM key_folder_scopes WHERE key_id=%s", (self.ids["key"],)).fetchall()
        self.assertEqual({r["folder_id"] for r in scopes}, {self.ids["shared"], self.ids["private"]})


if __name__ == "__main__":
    unittest.main()
