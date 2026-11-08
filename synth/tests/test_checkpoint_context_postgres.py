"""Checkpoint retrieval quality, provenance and permissions on real PostgreSQL.

The text is a synthetic fixture. These tests prove retrieval behavior, not ASR
quality or model-based meeting understanding.
"""
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException, Request

from synth.server import meeting_context
from synth.tests import test_questions as fixture

HISTORY = [("system:S1", "Buenos días. Comenzamos la revisión del proyecto."),
           ("system:S1", "Primero repasamos el orden del día y la próxima convocatoria."),
           ("system:S1", "Terminamos el repaso de asistencia y seguimos con los asuntos pendientes."),
           ("system:S2", "Los permisos de las carpetas se revisarán antes de desplegar en Nimbus.")]


@unittest.skipUnless(os.environ.get("SYNTH_TEST_DATABASE_URL"), "Disposable PostgreSQL URL required")
class CheckpointContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.QuestionFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        for name in ("db", "alice", "bob", "folder", "shared", "private", "api"):
            setattr(self, name, getattr(self.fixture, name))
        self.upload(self.shared, HISTORY)
        self.db.execute("UPDATE meetings SET created_at='2026-10-01' WHERE id IN (%s,%s)", (self.shared, self.private))
        self.active = self.api.create_meeting(self.api.Meeting(title="Revisión actual", folder_id=self.folder), (self.db, self.alice))["id"]
        self.db.execute("UPDATE meetings SET created_at='2026-11-05 10:00:00+00' WHERE id=%s", (self.active,))
        self.term("Nimbus", "sistema", "plataforma de despliegue", self.folder)
        self.term("Synth", "organizacion", "equipo", None)

    def upload(self, meeting_id, turns, speakers=None):
        from synth.contracts.transcript import Transcript
        content = fixture.transcript(turns)
        if speakers:
            content["speakers"] = speakers
        return self.api.store_transcript_version(self.db, self.alice, meeting_id, Transcript.model_validate(content),
                                                  uuid4().hex, "transcript.upload")

    def term(self, name, kind, definition, folder):
        from synth.server.glossary import TermIn
        route = self.fixture.route("POST", "/v1/glossary")
        return route(TermIn(term=name, kind=kind, definition=definition, folder_id=folder), (self.db, self.alice))

    def snapshot(self, query="", actor=None, meeting=None):
        return meeting_context.snapshot(self.db, actor or self.alice, meeting or self.active, query)

    def test_multiword_query_returns_relevant_turn_after_the_opening(self):
        context = self.snapshot("permisos Nimbus")
        self.assertEqual(len(context["passages"]), 1)
        passage = context["passages"][0]
        self.assertEqual(passage["turn_id"], "t000003")
        self.assertEqual(passage["text"], HISTORY[3][1])
        self.assertEqual(passage["transcript_version"], 2)
        self.assertEqual(passage["meeting_id"], str(self.shared))
        self.assertEqual(passage["evidence_id"], f"{self.shared}@2:t000003")
        self.assertEqual(passage["provenance"], "historical_transcript")
        self.assertEqual(passage["before_evidence_id"], f"{self.shared}@2:t000002")
        self.assertEqual(len(passage["content_hash"]), 64)
        self.assertEqual(context["usage"], {"text_inference_calls": 0, "audio_inference_calls": 0})

    def test_checkpoint_excludes_self_future_other_folders_and_private_history(self):
        self.upload(self.active, [("system:S1", "Nimbus es lo que estamos investigando ahora.")])
        future = self.api.create_meeting(self.api.Meeting(title="Posterior", folder_id=self.folder), (self.db, self.alice))["id"]
        self.upload(future, [("system:S1", "Nimbus contiene una decisión posterior.")])
        self.db.execute("UPDATE meetings SET created_at='2026-11-06' WHERE id=%s", (future,))
        elsewhere = uuid4()
        self.db.execute("INSERT INTO folders(id,owner_id,name) VALUES (%s,%s,'Otra carpeta')", (elsewhere, self.alice["id"]))
        other = self.api.create_meeting(self.api.Meeting(title="Otro proyecto", folder_id=elsewhere), (self.db, self.alice))["id"]
        self.upload(other, [("system:S1", "Nimbus contiene otro proyecto.")])
        self.term("Oculto", "proyecto", "Nimbus de otra carpeta", elsewhere)
        context = self.snapshot("Nimbus")
        self.assertEqual({p["meeting_id"] for p in context["passages"]}, {str(self.shared)})
        self.assertEqual([g["term"] for g in context["glossary"]], ["Nimbus"])

    def test_private_meeting_does_not_search_the_owners_entire_library(self):
        context = self.snapshot("Nimbus", meeting=self.private)
        self.assertEqual(context["scope"], "no_historical_folder")
        self.assertEqual(context["passages"], [])
        self.assertEqual(context["glossary"], [])  # folder glossary is outside this audience

    def test_initial_context_preserves_notes_revision_and_approved_folder_vocabulary(self):
        self.api.update_notes(self.active, self.api.Notes(notes="Preguntar por los permisos.", expected_revision=1), (self.db, self.alice))
        self.term("Dudoso", "producto", "no aprobado", self.folder)
        self.db.execute("UPDATE glossary_terms SET status='propuesto' WHERE term='Dudoso'")
        first = self.snapshot()
        self.assertEqual([g["term"] for g in first["glossary"]], ["Nimbus", "Synth"])
        self.assertEqual(first["human_notes"]["text"], "Preguntar por los permisos.")
        self.assertEqual(first["human_notes"]["revision"], 2)
        self.assertEqual(first["passages"], [])
        self.assertEqual(first["fingerprint"], self.snapshot()["fingerprint"])
        self.assertTrue(first["requires_fresh_authorization_before_reuse"])

    def test_no_hits_do_not_return_opening_passages_or_unrelated_vocabulary(self):
        context = self.snapshot("astrobiología telescopio")
        self.assertEqual((context["passages"], context["glossary"]), ([], []))

    def test_snapshot_bounds_passages_glossary_and_notes(self):
        for index in range(35):
            self.term(f"Sistema {index:02}", "sistema", "plataforma", self.folder)
        self.api.update_notes(self.active, self.api.Notes(notes="apunte "*1000, expected_revision=1), (self.db, self.alice))
        self.upload(self.shared, [("system:S1", "Nimbus permisos. "*100) for _ in range(12)])
        initial = self.snapshot()
        self.assertEqual(len(initial["glossary"]), 30)
        self.assertEqual(len(initial["human_notes"]["text"]), 2000)
        self.assertTrue(initial["human_notes"]["truncated"])
        enriched = self.snapshot("Nimbus permisos")
        self.assertEqual(len(enriched["passages"]), 8)
        self.assertTrue(all(len(p["text"]) <= 1200 and len(p["before"]) <= 300 for p in enriched["passages"]))

    def test_historical_names_never_confirm_current_participants(self):
        self.upload(self.shared, HISTORY, {"system:S2": {"status": "confirmed", "name": "Jeremie"}})
        context = self.snapshot("Nimbus")
        self.assertEqual(context["passages"][0]["speaker"], "Jeremie")
        self.assertEqual(context["participants_confirmed_in_this_meeting"], [])
        self.upload(self.active, [("system:S1", "Comenzamos.")], {"system:S1": {"status": "renamed", "name": "Ana"}})
        self.assertEqual(self.snapshot()["participants_confirmed_in_this_meeting"], ["Ana"])

    def test_historical_recorder_is_not_presented_as_the_current_user(self):
        self.upload(self.shared, [("microphone:local", "Revisaré los permisos de Nimbus.")])
        context = self.snapshot("Nimbus")
        self.assertEqual(context["passages"][0]["speaker"], "Persona que grabó esa reunión")
        self.assertIsNone(context["passages"][0]["before_evidence_id"])

    def test_notes_and_glossary_edits_invalidate_the_context_fingerprint(self):
        initial = self.snapshot()
        self.api.update_notes(self.active, self.api.Notes(notes="Apunte nuevo", expected_revision=1), (self.db, self.alice))
        notes = self.snapshot()
        self.assertNotEqual(initial["fingerprint"], notes["fingerprint"])
        self.db.execute("UPDATE glossary_terms SET definition='otro significado' WHERE term='Nimbus'")
        glossary = self.snapshot()
        self.assertNotEqual(notes["fingerprint"], glossary["fingerprint"])
        self.db.execute("UPDATE glossary_terms SET status='descartado' WHERE term='Nimbus'")
        self.assertNotIn("Nimbus", [g["term"] for g in self.snapshot()["glossary"]])

    def test_revocation_refuses_a_fresh_checkpoint_even_for_the_meeting_owner(self):
        self.db.execute("INSERT INTO folder_memberships VALUES (%s,%s,'editor')", (self.folder, self.bob["id"]))
        active = self.api.create_meeting(self.api.Meeting(title="De Bob", folder_id=self.folder), (self.db, self.bob))["id"]
        self.assertEqual(self.snapshot("Nimbus", self.bob, active)["folder_id"], str(self.folder))
        self.db.execute("DELETE FROM folder_memberships WHERE principal_id=%s", (self.bob["id"],))
        with self.assertRaises(HTTPException) as denied:
            self.snapshot("Nimbus", self.bob, active)
        self.assertEqual(denied.exception.status_code, 404)

    def test_source_edits_and_moves_change_fingerprint_and_remove_stale_content(self):
        first = self.snapshot("Nimbus")
        self.upload(self.shared, [("system:S1", "Nimbus continúa en revisión, no hay acuerdo.")])
        second = self.snapshot("Nimbus")
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(second["passages"][0]["transcript_version"], 3)
        self.assertNotEqual(first["passages"][0]["content_hash"], second["passages"][0]["content_hash"])
        self.db.execute("UPDATE meetings SET folder_id=NULL WHERE id=%s", (self.shared,))
        third = self.snapshot("Nimbus")
        self.assertEqual(third["passages"], [])
        self.assertNotEqual(second["fingerprint"], third["fingerprint"])

    def test_mcp_tool_is_read_only_validates_args_and_respects_key_scope(self):
        key = uuid4()
        machine = {"id": uuid4(), "kind": "machine", "name": "Checkpoint", "key_id": key}
        self.db.execute("INSERT INTO principals(id,name,kind) VALUES (%s,'Checkpoint','machine')", (machine["id"],))
        self.db.execute("INSERT INTO access_keys(id,principal_id,token_hash) VALUES (%s,%s,%s)", (key, machine["id"], uuid4().hex))
        self.db.execute("INSERT INTO folder_memberships VALUES (%s,%s,'reader')", (self.folder, machine["id"]))
        request = Request({"type": "http", "headers": []})
        def call(args):
            rpc = self.api.RPC(jsonrpc="2.0", id=1, method="tools/call", params={"name": "get_meeting_context", "arguments": args})
            return self.api.mcp(rpc, (self.db, machine), request)["result"]
        args = {"meeting_id": str(self.active), "query": "Nimbus"}
        self.assertTrue(call(args)["isError"])
        self.db.execute("INSERT INTO key_folder_scopes VALUES (%s,%s)", (key, self.folder))
        allowed = call(args)
        self.assertFalse(allowed["isError"])
        self.assertEqual(json.loads(allowed["content"][0]["text"])["passages"][0]["meeting_id"], str(self.shared))
        self.assertTrue(call({**args, "folder_id": str(self.folder)})["isError"])
        self.assertTrue(call({**args, "query": "x"*501})["isError"])
        tool = next(t for t in self.api.MCP_TOOLS if t["name"] == "get_meeting_context")
        self.assertTrue(tool["annotations"]["readOnlyHint"])

    def test_http_requires_auth_and_the_same_folder_permissions(self):
        from fastapi.testclient import TestClient
        from synth.server.db import digest
        token = uuid4().hex
        self.db.execute("INSERT INTO access_keys(id,principal_id,token_hash) VALUES (%s,%s,%s)", (uuid4(), self.bob["id"], digest(token)))
        self.db.commit()
        with patch.object(self.api, "connect", self.fixture.connect):
            client = TestClient(self.api.app)
            path = f"/v1/meetings/{self.active}/context?q=Nimbus"
            self.assertEqual(client.get(path).status_code, 401)
            headers = {"Authorization": "Bearer " + token}
            self.assertEqual(client.get(path, headers=headers).status_code, 404)
            self.db.execute("INSERT INTO folder_memberships VALUES (%s,%s,'reader')", (self.folder, self.bob["id"]))
            self.db.commit()
            response = client.get(path, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.json()["passages"][0]["meeting_id"], str(self.shared))


if __name__ == "__main__":
    unittest.main()
