"""Additive migrations apply twice, roll back cleanly and re-apply."""
import os
import unittest
from pathlib import Path
from uuid import uuid4

SERVER = Path(__file__).resolve().parents[1] / "server"


CURRENT = list(range(1, 16))


@unittest.skipUnless(os.environ.get("SYNTH_TEST_DATABASE_URL"), "Disposable PostgreSQL URL required")
class MigrationTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        url = os.environ["SYNTH_TEST_DATABASE_URL"]
        if "@127.0.0.1:18289/" not in url:
            self.fail("Tests require the dedicated disposable local PostgreSQL port")
        self.schema = "migrations_" + uuid4().hex
        self.db = psycopg.connect(url, autocommit=True)
        self.db.execute("CREATE SCHEMA " + self.schema)
        self.db.execute("SET search_path=" + self.schema)

    def tearDown(self):
        self.db.execute("DROP SCHEMA " + self.schema + " CASCADE")
        self.db.close()

    def versions(self):
        return [r[0] for r in self.db.execute("SELECT version FROM schema_versions ORDER BY version").fetchall()]

    def table(self, name):
        return self.db.execute("SELECT to_regclass(%s)", (self.schema + "." + name,)).fetchone()[0]

    def test_up_twice_down_and_up_again(self):
        schema = (SERVER / "schema.sql").read_text()
        self.db.execute(schema)
        self.db.execute(schema)
        self.assertEqual(self.versions(), CURRENT)
        self.db.execute((SERVER / "migrations/0012_document_generations.down.sql").read_text())
        self.db.execute((SERVER / "migrations/0011_alias_feedback.down.sql").read_text())
        self.assertIsNone(self.table("correction_reviews"))
        self.db.execute((SERVER / "migrations/0010_rls_per_actor.down.sql").read_text())
        self.assertIsNone(self.db.execute("SELECT to_regprocedure(%s)", (self.schema + ".rls_actor()",)).fetchone()[0])
        self.db.execute((SERVER / "migrations/0009_glossary_kinds.down.sql").read_text())
        self.db.execute((SERVER / "migrations/0008_questions.down.sql").read_text())
        self.assertIsNone(self.table("questions"))
        self.db.execute((SERVER / "migrations/0007_glossary.down.sql").read_text())
        self.db.execute((SERVER / "migrations/0006_tasks_decisions.down.sql").read_text())
        # v13-v15 have no down scripts; everything with one is rolled back to v5.
        self.assertEqual([v for v in self.versions() if v <= 12], [1, 2, 3, 4, 5])
        self.assertIsNone(self.table("tasks"))
        self.assertIsNone(self.table("glossary_terms"))
        self.assertIsNotNone(self.table("meetings"))
        self.db.execute(schema)
        self.assertEqual(self.versions(), CURRENT)

    def test_v9_maps_old_kinds_to_the_taxonomy_and_back(self):
        import psycopg
        schema = (SERVER / "schema.sql").read_text()
        self.db.execute(schema)
        self.db.execute((SERVER / "migrations/0009_glossary_kinds.down.sql").read_text())
        insert = "INSERT INTO glossary_terms(id,scope,folder_id,term,kind) VALUES (%s,'workspace',NULL,%s,%s)"
        for term, kind in (("Valdivar", "empresa"), ("OKR", "sigla"), ("Atlas", "proyecto")):
            self.db.execute(insert, (uuid4(), term, kind))
        self.db.execute(schema)
        kinds = dict(self.db.execute("SELECT term, kind FROM glossary_terms").fetchall())
        self.assertEqual(kinds, {"Valdivar": "organizacion", "OKR": "concepto", "Atlas": "proyecto"})
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.db.execute(insert, (uuid4(), "Vale", "otro"))

    def test_glossary_scope_and_uniqueness_are_enforced(self):
        import psycopg
        self.db.execute((SERVER / "schema.sql").read_text())
        insert = "INSERT INTO glossary_terms(id,scope,folder_id,term) VALUES (%s,%s,%s,%s)"
        self.db.execute(insert, (uuid4(), "workspace", None, "Valdivar"))
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.db.execute(insert, (uuid4(), "workspace", None, "valdivar"))
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.db.execute(insert, (uuid4(), "folder", None, "Lucerno"))


if __name__ == "__main__":
    unittest.main()
