"""The old miner's proposals are re-classified once; entities stay with what they are, noise is discarded."""
import json
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from synth.worker import glossary_triage


class FakeModel:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, messages, tokens):
        self.calls.append(messages)
        return {"content": self.answer if isinstance(self.answer, str) else json.dumps(self.answer)}


@unittest.skipUnless(os.environ.get("SYNTH_TEST_DATABASE_URL"), "Disposable PostgreSQL URL required")
class TriageTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        url = os.environ["SYNTH_TEST_DATABASE_URL"]
        if "@127.0.0.1:18289/" not in url:
            self.fail("Tests require the dedicated disposable local PostgreSQL port")
        self.schema = "triage_" + uuid4().hex
        self.admin = psycopg.connect(url, autocommit=True)
        self.admin.execute("CREATE SCHEMA " + self.schema)
        self.db = psycopg.connect(url, row_factory=dict_row, autocommit=True, options="-c search_path=" + self.schema)
        self.db.execute((Path(__file__).resolve().parents[1] / "server/schema.sql").read_text())
        owner = uuid4()
        self.db.execute("INSERT INTO principals(id,name,kind) VALUES (%s,'Owner','user')", (owner,))
        self.folder = uuid4()
        self.db.execute("INSERT INTO folders(id,owner_id,name) VALUES (%s,%s,'Clientes')", (self.folder, owner))

    def tearDown(self):
        self.db.close()
        self.admin.execute("DROP SCHEMA " + self.schema + " CASCADE")
        self.admin.close()

    @contextmanager
    def connect(self):
        yield self.db

    def add(self, term, origin="fonetico", status="propuesto", definition="", examples=(), occurrences=1):
        self.db.execute("""INSERT INTO glossary_terms(id,scope,folder_id,term,status,origin,definition,examples,occurrences)
            VALUES (%s,'folder',%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                        (uuid4(), self.folder, term, status, origin, definition, json.dumps(list(examples)), occurrences))

    def terms(self):
        return {r["term"]: (r["status"], r["kind"], r["definition"], r["occurrences"]) for r in
                self.db.execute("SELECT term,status,kind,definition,occurrences FROM glossary_terms").fetchall()}

    def test_entities_are_kept_with_what_they_are_and_noise_is_discarded(self):
        self.add("Atlántica", examples=["Mañana vemos a Atlántica para el piloto."])
        self.add("¿Vale?", examples=["¿Vale? Seguimos."])
        self.add("Ventolia", examples=["Seguimos con el piloto."])      # the model calls it an entity, but it is not in the examples
        self.add("Clientes", origin="carpeta")                            # no example to ground it
        self.add("Baldibar", definition="Posible alias de g1")              # an uncertain alias proposal of the new miner: not swept
        self.add("Lucerno", status="aprobado", definition="cliente de logística")
        model = FakeModel({"terms": [
            {"n": 1, "entity": True, "kind": "organización", "definition": "cliente del piloto"},
            {"n": 2, "entity": False},
            {"n": 3, "entity": True, "kind": "organizacion", "definition": "eléctrica"},
            {"n": 4, "entity": True, "kind": "concepto", "definition": "carpeta"}]})
        self.assertTrue(glossary_triage.step(self.connect, model))
        self.assertFalse(glossary_triage.step(self.connect, model))
        terms = self.terms()
        self.assertEqual(terms["Atlántica"][:3], ("aprobado", "organizacion", "cliente del piloto"))
        self.assertEqual({t: terms[t][0] for t in ("¿Vale?", "Ventolia", "Clientes")}, {"¿Vale?": "descartado", "Ventolia": "descartado",
                                                                                        "Clientes": "descartado"})
        self.assertEqual((terms["Baldibar"][0], terms["Lucerno"][:3]), ("propuesto", ("aprobado", "concepto", "cliente de logística")))
        self.assertEqual(len(model.calls), 1)

    def test_a_second_spelling_of_a_kept_entity_counts_on_it_instead_of_duplicating(self):
        self.add("Atlántica", examples=["vemos a Atlántica"], occurrences=2)
        self.add("Atlántica.", examples=["con Atlántica."], occurrences=3)
        entity = {"entity": True, "kind": "organizacion", "definition": "cliente del piloto"}
        glossary_triage.step(self.connect, FakeModel({"terms": [{"n": 1, **entity}, {"n": 2, **entity}]}))
        terms = self.terms()
        self.assertEqual(terms["Atlántica"][0::3], ("aprobado", 5))
        self.assertEqual(terms["Atlántica."][0], "descartado")

    def test_an_unreadable_answer_changes_nothing(self):
        self.add("Atlántica", examples=["vemos a Atlántica"])
        with self.assertRaises(ValueError):
            glossary_triage.step(self.connect, FakeModel("no es json"))
        self.assertEqual(self.terms()["Atlántica"][0], "propuesto")


if __name__ == "__main__":
    unittest.main()
