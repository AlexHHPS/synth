"""Glossary API against a disposable PostgreSQL; 401 without session."""
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException

from synth.server import app as api
from synth.server.glossary import Candidate, Merge, TermIn, TermPatch


def route(method, path):
    return next(r.endpoint for r in api.app.routes if getattr(r, "path", "") == path and method in r.methods)


@unittest.skipUnless(os.environ.get("SYNTH_TEST_DATABASE_URL"), "Disposable PostgreSQL URL required")
class GlossaryApiTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        url = os.environ["SYNTH_TEST_DATABASE_URL"]
        if "@127.0.0.1:18289/" not in url:
            self.fail("Tests require the dedicated disposable local PostgreSQL port")
        self.schema = "glossary_" + uuid4().hex
        self.admin = psycopg.connect(url, autocommit=True)
        self.admin.execute("CREATE SCHEMA " + self.schema)
        self.db = psycopg.connect(url, row_factory=dict_row, options="-c search_path=" + self.schema)
        self.db.execute((Path(__file__).resolve().parents[1] / "server/schema.sql").read_text())
        self.alice = {"id": uuid4(), "kind": "user", "name": "Alice"}
        self.bob = {"id": uuid4(), "kind": "user", "name": "Bob"}
        for actor in (self.alice, self.bob):
            self.db.execute("INSERT INTO principals(id,name,kind) VALUES (%s,%s,%s)", (actor["id"], actor["name"], "user"))
        self.folder = uuid4()
        self.db.execute("INSERT INTO folders(id,owner_id,name) VALUES (%s,%s,%s)", (self.folder, self.alice["id"], "Clientes"))

    def tearDown(self):
        self.db.close()
        self.admin.execute("DROP SCHEMA " + self.schema + " CASCADE")
        self.admin.close()

    def ctx(self, actor):
        return (self.db, actor)

    def test_create_propose_approve_merge_discard(self):
        valdivar = route("POST", "/v1/glossary")(TermIn(term="Valdivar", kind="organizacion", definition="cliente industrial"), self.ctx(self.alice))
        self.assertEqual(valdivar["status"], "aprobado")
        proposed = route("POST", "/v1/glossary/candidates")(Candidate(term="Baldibar", origin="fonetico", examples=["con Baldibar"]), self.ctx(self.alice))
        self.assertEqual(proposed["status"], "propuesto")
        again = route("POST", "/v1/glossary/candidates")(Candidate(term="baldibar", origin="fonetico", occurrences=2), self.ctx(self.alice))
        self.assertEqual(again["occurrences"], 3)
        merged = route("POST", "/v1/glossary/{term_id}/merge")(proposed["id"], Merge(into=valdivar["id"]), self.ctx(self.alice))
        self.assertIn("Baldibar", merged["aliases"])
        listed = route("GET", "/v1/glossary")(self.ctx(self.bob), None, None)["items"]
        self.assertEqual({t["term"]: t["status"] for t in listed}, {"Valdivar": "aprobado", "Baldibar": "descartado"})
        lucerno = route("POST", "/v1/glossary/candidates")(Candidate(term="Lucerno", origin="llm"), self.ctx(self.bob))
        self.assertEqual(route("POST", "/v1/glossary/{term_id}/approve")(lucerno["id"], self.ctx(self.bob))["status"], "aprobado")
        self.assertEqual(route("POST", "/v1/glossary/{term_id}/discard")(lucerno["id"], self.ctx(self.bob))["status"], "descartado")
        edited = route("PATCH", "/v1/glossary/{term_id}")(valdivar["id"], TermPatch(aliases=["Baldivar", "Baldivar"]), self.ctx(self.alice))
        self.assertEqual(edited["aliases"], ["Baldivar"])

    def test_workspace_terms_never_keep_meeting_excerpts_that_every_employee_would_read(self):
        from synth.server.glossary import upsert_candidate, upsert_entity
        proposed = route("POST", "/v1/glossary/candidates")(Candidate(term="Baldibar", origin="fonetico",
                                                                     examples=["en mi uno a uno con Baldibar"]), self.ctx(self.alice))
        self.assertEqual(proposed["examples"], [])
        self.assertEqual(upsert_candidate(self.db, None, "Baldibar", "llm", None, examples=["otra frase"])["examples"], [])
        self.assertEqual(upsert_entity(self.db, "Atlas", "proyecto", "iniciativa interna", None, ["dijo Atlas"])["examples"], [])
        in_folder = upsert_entity(self.db, "Atlas", "proyecto", "iniciativa interna", self.folder, ["dijo Atlas"])
        self.assertEqual(in_folder["examples"], ["dijo Atlas"])

    def test_adding_an_existing_term_again_keeps_it_approved_with_its_aliases(self):
        first = route("POST", "/v1/glossary")(TermIn(term="Valdivar", kind="organizacion"), self.ctx(self.alice))
        again = route("POST", "/v1/glossary")(TermIn(term="valdivar"), self.ctx(self.alice))
        self.assertEqual((again["id"], again["status"], again["aliases"]), (first["id"], "aprobado", []))
        aliased = route("POST", "/v1/glossary")(TermIn(term="Valdivar", aliases=["Baldivar"]), self.ctx(self.alice))
        self.assertEqual(aliased["aliases"], ["Baldivar"])

    def test_spellings_that_differ_in_punctuation_accents_or_case_are_one_term(self):
        from synth.server.glossary import upsert_candidate
        first = upsert_candidate(self.db, self.alice["id"], "¿Atlántica?", "llm", self.folder)
        self.assertEqual(first["term"], "Atlántica")
        for spelling in ("atlantica", "Atlántica.", "«ATLÁNTICA»"):
            upsert_candidate(self.db, self.alice["id"], spelling, "llm", self.folder)
        rows = self.db.execute("SELECT term, occurrences FROM glossary_terms").fetchall()
        self.assertEqual([(r["term"], r["occurrences"]) for r in rows], [("Atlántica", 4)])
        # An alias spelling counts on its term instead of becoming a new proposal.
        valdivar = route("POST", "/v1/glossary")(TermIn(term="Valdivar", aliases=["Baldibar"], folder_id=self.folder), self.ctx(self.alice))
        self.assertEqual(upsert_candidate(self.db, None, "¿Baldibar?", "fonetico", self.folder)["id"], valdivar["id"])
        again = route("POST", "/v1/glossary")(TermIn(term="¿valdivar?", folder_id=self.folder), self.ctx(self.alice))
        self.assertEqual((again["id"], again["kind"]), (valdivar["id"], "concepto"))

    def test_entities_are_added_approved_with_what_they_are_and_deleted_ones_stay_deleted(self):
        from synth.server.glossary import upsert_candidate, upsert_entity
        nimbus = upsert_entity(self.db, "Nimbus", "sistema", "plataforma de despliegue", self.folder, ["usar Nimbus"])
        self.assertEqual((nimbus["status"], nimbus["origin"], nimbus["kind"], nimbus["definition"]),
                         ("aprobado", "llm", "sistema", "plataforma de despliegue"))
        proposal = upsert_candidate(self.db, None, "Atlas", "llm", self.folder)
        atlas = upsert_entity(self.db, "atlas", "proyecto", "migración de nóminas", self.folder)
        self.assertEqual((atlas["id"], atlas["status"], atlas["kind"]), (proposal["id"], "aprobado", "proyecto"))
        route("POST", "/v1/glossary/{term_id}/discard")(nimbus["id"], self.ctx(self.alice))
        self.assertEqual(upsert_entity(self.db, "Nimbus", "sistema", "otra cosa", self.folder)["status"], "descartado")
        manual = route("POST", "/v1/glossary")(TermIn(term="Lucerno", definition="cliente de logística", folder_id=self.folder), self.ctx(self.alice))
        kept = upsert_entity(self.db, "Lucerno", "organizacion", "otra definición", self.folder)
        self.assertEqual((kept["id"], kept["definition"]), (manual["id"], "cliente de logística"))

    def test_renaming_keeps_the_heard_spelling_as_an_alias_or_merges_into_the_existing_term(self):
        patch = route("PATCH", "/v1/glossary/{term_id}")
        heard = route("POST", "/v1/glossary")(TermIn(term="Superbase", kind="sistema"), self.ctx(self.alice))
        renamed = patch(heard["id"], TermPatch(term="Supabase"), self.ctx(self.alice))
        self.assertEqual((renamed["id"], renamed["term"], renamed["aliases"]), (heard["id"], "Supabase", ["Superbase"]))
        clubfer = route("POST", "/v1/glossary")(TermIn(term="Clubfer"), self.ctx(self.alice))
        cloudflare = route("POST", "/v1/glossary")(TermIn(term="Cloudflare", definition="CDN y WAF"), self.ctx(self.alice))
        merged = patch(clubfer["id"], TermPatch(term="cloudflare"), self.ctx(self.alice))
        self.assertEqual((merged["id"], merged["aliases"], merged["definition"]), (cloudflare["id"], ["Clubfer"], "CDN y WAF"))
        statuses = {r["term"]: r["status"] for r in self.db.execute("SELECT term, status FROM glossary_terms").fetchall()}
        self.assertEqual(statuses["Clubfer"], "descartado")
        # Changing only the capitals is not a new spelling to remember.
        self.assertEqual(patch(cloudflare["id"], TermPatch(term="CloudFlare"), self.ctx(self.alice))["aliases"], ["Clubfer"])

    def test_an_extracted_name_that_sounds_like_a_term_becomes_its_alias(self):
        from synth.server.glossary import upsert_entity
        omniroute = route("POST", "/v1/glossary")(TermIn(term="OmniRoute", kind="sistema", folder_id=self.folder), self.ctx(self.alice))
        heard = upsert_entity(self.db, "OmniRoot", "sistema", "gateway", self.folder)
        self.assertEqual((heard["id"], heard["aliases"]), (omniroute["id"], ["OmniRoot"]))
        self.assertEqual(self.db.execute("SELECT count(*) AS n FROM glossary_terms").fetchone()["n"], 1)
        # A different name is a new term; a workspace term is never extended from a folder meeting.
        route("POST", "/v1/glossary")(TermIn(term="Lucerno"), self.ctx(self.alice))
        atlas = upsert_entity(self.db, "Atlas", "proyecto", "migración de nóminas", self.folder)
        luservo = upsert_entity(self.db, "Luservo", "organizacion", "cliente", self.folder)
        self.assertEqual((atlas["term"], luservo["term"], luservo["scope"]), ("Atlas", "Luservo", "folder"))

    def test_folder_terms_follow_folder_access(self):
        route("POST", "/v1/glossary")(TermIn(term="Proyecto Atlas", folder_id=self.folder), self.ctx(self.alice))
        with self.assertRaises(HTTPException) as raised:
            route("GET", "/v1/glossary")(self.ctx(self.bob), self.folder, None)
        self.assertEqual(raised.exception.status_code, 404)
        self.assertNotIn("Proyecto Atlas", [t["term"] for t in route("GET", "/v1/glossary")(self.ctx(self.bob), None, None)["items"]])
        with self.assertRaises(HTTPException):
            route("POST", "/v1/glossary")(TermIn(term="Otro", folder_id=self.folder), self.ctx(self.bob))

    def test_folder_term_cannot_be_merged_into_a_workspace_term(self):
        valdivar = route("POST", "/v1/glossary")(TermIn(term="Valdivar"), self.ctx(self.alice))
        secret = route("POST", "/v1/glossary/candidates")(Candidate(term="Proyecto Atlas", origin="llm", folder_id=self.folder), self.ctx(self.alice))
        with self.assertRaises(HTTPException) as raised:
            route("POST", "/v1/glossary/{term_id}/merge")(secret["id"], Merge(into=valdivar["id"]), self.ctx(self.alice))
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(route("GET", "/v1/glossary")(self.ctx(self.bob), None, None)["items"][0]["aliases"], [])

    def test_common_words_never_become_aliases(self):
        valdivar = route("POST", "/v1/glossary")(TermIn(term="Valdivar"), self.ctx(self.alice))
        edited = route("PATCH", "/v1/glossary/{term_id}")(valdivar["id"], TermPatch(aliases=["cosas", "Baldivar"]), self.ctx(self.alice))
        self.assertEqual(edited["aliases"], ["Baldivar"])

    def test_integrations_can_only_read_approved_terms(self):
        route("POST", "/v1/glossary/candidates")(Candidate(term="Luservo", origin="fonetico"), self.ctx(self.alice))
        machine = {"id": uuid4(), "kind": "machine", "name": "MCP", "key_id": uuid4()}
        self.assertEqual(route("GET", "/v1/glossary")(self.ctx(machine), None, None)["items"], [])
        with self.assertRaises(HTTPException) as raised:
            route("POST", "/v1/glossary")(TermIn(term="X"), self.ctx(machine))
        self.assertEqual(raised.exception.status_code, 403)


class GlossaryNoSessionTests(unittest.TestCase):
    def test_every_glossary_endpoint_requires_a_session(self):
        from fastapi.testclient import TestClient

        @contextmanager
        def fake_connect():
            yield object()
        term = uuid4()
        calls = [("GET", "/v1/glossary", None), ("POST", "/v1/glossary", {"term": "x"}),
                 ("POST", "/v1/glossary/candidates", {"term": "x", "origin": "llm"}),
                 ("POST", f"/v1/glossary/{term}/approve", None), ("POST", f"/v1/glossary/{term}/discard", None),
                 ("PATCH", f"/v1/glossary/{term}", {"definition": "d"}), ("POST", f"/v1/glossary/{term}/merge", {"into": str(uuid4())})]
        with patch("synth.server.app.connect", fake_connect):
            client = TestClient(api.app)
            for method, path, body in calls:
                with self.subTest(path=path, method=method):
                    self.assertEqual(client.request(method, path, json=body).status_code, 401)


if __name__ == "__main__":
    unittest.main()
