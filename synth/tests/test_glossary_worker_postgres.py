"""Worker end to end: raw version untouched, corrected version + minutes, proposals, undo."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from synth.tests.test_minutes import GOOD_TOPIC_1, GOOD_TOPIC_2, TRANSCRIPT, FakeModel


class GlossaryAwareModel(FakeModel):
    def __call__(self, messages, max_tokens):
        if "¿lo oído es el término?" in messages[1]["content"]:
            self.calls.append(messages)
            ids = [line.split(":")[0] for line in messages[1]["content"].split("\n") if line.startswith("k")]
            return {"content": json.dumps({"decisions": [{"id": i, "apply": True} for i in ids],
                                           "doubtful": [{"heard": "Atlantia", "guess": "Atlántica"}]}), "effective_model": "fake"}
        return super().__call__(messages, max_tokens)


@unittest.skipUnless(os.environ.get("SYNTH_TEST_DATABASE_URL"), "Disposable PostgreSQL URL required")
class GlossaryWorkerTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from synth.server import app as api
        from synth.contracts.transcript import Transcript
        self.api = api
        url = os.environ["SYNTH_TEST_DATABASE_URL"]
        if "@127.0.0.1:18289/" not in url:
            self.fail("Tests require the dedicated disposable local PostgreSQL port")
        self.url, self.schema = url, "gw_" + uuid4().hex
        self.admin = psycopg.connect(url, autocommit=True)
        self.admin.execute("CREATE SCHEMA " + self.schema)
        self.db = self.connect()
        self.db.execute((Path(__file__).resolve().parents[1] / "server/schema.sql").read_text())
        self.owner = {"id": uuid4(), "kind": "user", "name": "Owner"}
        self.db.execute("INSERT INTO principals(id,name,kind) VALUES (%s,%s,%s)", (self.owner["id"], "Owner", "user"))
        self.folder = uuid4()
        self.db.execute("INSERT INTO folders(id,owner_id,name) VALUES (%s,%s,%s)", (self.folder, self.owner["id"], "Clientes"))
        self.mid = api.create_meeting(api.Meeting(title="Comité", folder_id=self.folder), (self.db, self.owner))["id"]
        self.db.execute("INSERT INTO glossary_terms(id,scope,term,status) VALUES (%s,'workspace','Valdivar','aprobado')", (uuid4(),))
        raw = json.loads(json.dumps({k: v for k, v in TRANSCRIPT.items() if k != "speakers"}))
        raw["segments"][0]["text"] = "Soy Pedro y propongo pasar el piloto de Baldibar al 15 de noviembre."
        raw["turns"][0]["text"] = raw["segments"][0]["text"]
        self.raw = raw
        api.store_transcript_version(self.db, self.owner, self.mid, Transcript.model_validate(raw), "k1", "transcript.upload")
        self.db.commit()

    def connect(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.url, row_factory=dict_row, options="-c search_path=" + self.schema)

    def tearDown(self):
        self.db.close()
        self.admin.execute("DROP SCHEMA " + self.schema + " CASCADE")
        self.admin.close()

    def test_a_registered_alias_of_the_company_glossary_is_corrected_in_any_meeting_and_can_be_undone(self):
        self.db.execute("UPDATE glossary_terms SET aliases='{Baldibar}' WHERE term='Valdivar'")
        self.db.execute("UPDATE meetings SET folder_id=NULL WHERE id=%s", (self.mid,))  # a private meeting too
        self.db.commit()
        self.run_job()
        versions = self.db.execute("SELECT version, content FROM transcript_versions WHERE meeting_id=%s ORDER BY version", (self.mid,)).fetchall()
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertIn("Baldibar", versions[0]["content"]["segments"][0]["text"])  # the raw version stays as heard
        self.assertIn("Valdivar", versions[1]["content"]["segments"][0]["text"])
        self.assertEqual([(c["original"], c["origin"]) for c in versions[1]["content"]["corrections"]], [("Baldibar", "alias")])
        document = self.db.execute("SELECT transcript_version FROM document_versions WHERE meeting_id=%s", (self.mid,)).fetchone()
        self.assertEqual(document["transcript_version"], 2)
        undo = next(r.endpoint for r in self.api.app.routes if getattr(r, "path", "").endswith("/undo"))
        self.assertEqual(undo(self.mid, "c0001", (self.db, self.owner))["transcript_version"], 3)

    def route(self, method, suffix):
        return next(r.endpoint for r in self.api.app.routes if getattr(r, "path", "").endswith(suffix) and method in r.methods)

    def suggest_cinta(self):
        self.db.execute("INSERT INTO glossary_terms(id,scope,term,status,aliases) VALUES (%s,'workspace','Synth','aprobado','{Cinta}')", (uuid4(),))
        raw = json.loads(json.dumps(self.raw))
        raw["segments"][0]["text"] = raw["turns"][0]["text"] = "Soy Pedro y en Cinta probamos el glosario."
        from synth.contracts.transcript import Transcript
        self.api.store_transcript_version(self.db, self.owner, self.mid, Transcript.model_validate(raw), "k2", "transcript.upload")
        self.db.commit()
        self.db.execute("UPDATE jobs SET state='cancelled' WHERE payload->>'transcript_version'='1'")
        self.db.commit()
        self.run_job()
        latest = self.api.transcript(self.mid, (self.db, self.owner))
        return latest, next(c for c in latest["content"]["corrections"] if c["status"] == "suggested")

    def feedback(self):
        return self.db.execute("SELECT alias_feedback FROM glossary_terms WHERE term='Synth'").fetchone()["alias_feedback"]

    def test_an_ambiguous_alias_is_suggested_and_accepting_it_makes_a_new_version_and_counts(self):
        latest, suggestion = self.suggest_cinta()
        self.assertIn("en Cinta probamos", latest["content"]["segments"][0]["text"])  # a suggestion leaves the text as heard
        result = self.route("POST", "/accept")(self.mid, suggestion["id"], (self.db, self.owner))
        accepted = self.api.transcript(self.mid, (self.db, self.owner))
        self.assertEqual(accepted["version"], result["transcript_version"])
        self.assertIn("en Synth probamos", accepted["content"]["segments"][0]["text"])
        self.assertEqual(self.feedback(), {"cinta": {"si": 1}})

    def test_rejecting_a_suggestion_keeps_the_version_hides_it_and_counts_once(self):
        from fastapi import HTTPException
        latest, suggestion = self.suggest_cinta()
        for _ in range(2):
            self.assertEqual(self.route("POST", "/reject")(self.mid, suggestion["id"], (self.db, self.owner))["status"], "rejected")
        again = self.api.transcript(self.mid, (self.db, self.owner))
        self.assertEqual(again["version"], latest["version"])
        self.assertEqual(next(c["status"] for c in again["content"]["corrections"] if c["id"] == suggestion["id"]), "rejected")
        self.assertEqual(self.feedback(), {"cinta": {"no": 1}})
        with self.assertRaises(HTTPException) as refused:
            self.route("POST", "/accept")(self.mid, suggestion["id"], (self.db, self.owner))
        self.assertEqual(refused.exception.status_code, 409)

    def test_recurring_invented_misspellings_become_aliases_of_their_term_and_real_words_do_not(self):
        from synth.worker.minutes_store import learn_aliases
        company = self.db.execute("SELECT id FROM glossary_terms WHERE term='Valdivar'").fetchone()["id"]
        add = """INSERT INTO glossary_terms(id,scope,folder_id,term,status,origin,occurrences,definition)
                 VALUES (%s,'folder',%s,%s,'propuesto','fonetico',%s,%s)"""
        self.db.execute(add, (uuid4(), self.folder, "Baldibar", 2, f"Posible alias de {company}"))
        self.db.execute(add, (uuid4(), self.folder, "Gumbary", 1, f"Posible alias de {company}"))   # heard once: not yet
        self.db.execute(add, (uuid4(), self.folder, "Pedro", 3, f"Posible alias de {company}"))     # a real name: never alone
        self.assertEqual(learn_aliases(self.db, self.folder), 1)
        self.assertEqual(self.db.execute("SELECT aliases FROM glossary_terms WHERE id=%s", (company,)).fetchone()["aliases"], ["Baldibar"])
        statuses = {r["term"]: r["status"] for r in self.db.execute("SELECT term, status FROM glossary_terms WHERE origin='fonetico'").fetchall()}
        self.assertEqual(statuses, {"Baldibar": "descartado", "Gumbary": "propuesto", "Pedro": "propuesto"})
        self.assertEqual(learn_aliases(self.db, None), 0)  # a private meeting teaches the shared glossary nothing

    @patch.dict(os.environ, {"SYNTH_GLOSSARY_ALIASES": "0"})
    def test_alias_correction_can_be_switched_off(self):
        self.db.execute("UPDATE glossary_terms SET aliases='{Baldibar}' WHERE term='Valdivar'")
        self.db.commit()
        self.run_job()
        versions = [r["version"] for r in self.db.execute("SELECT version FROM transcript_versions WHERE meeting_id=%s", (self.mid,)).fetchall()]
        self.assertEqual(versions, [1])

    def test_without_a_registered_alias_the_raw_transcript_is_kept(self):
        self.run_job()
        versions = [r["version"] for r in self.db.execute("SELECT version FROM transcript_versions WHERE meeting_id=%s", (self.mid,)).fetchall()]
        self.assertEqual(versions, [1])
        document = self.db.execute("SELECT transcript_version FROM document_versions WHERE meeting_id=%s", (self.mid,)).fetchone()
        self.assertEqual(document["transcript_version"], 1)

    @patch.dict(os.environ, {"SYNTH_GLOSSARY_AUTOCORRECT": "1"})
    def test_correction_is_a_new_version_with_minutes_and_proposals(self):
        from synth.worker import main, queue
        claimed = queue.claim(self.db, main.WORKER, 300)
        self.db.commit()
        model = GlossaryAwareModel([GOOD_TOPIC_1, GOOD_TOPIC_2])
        with patch.object(main, "connect", side_effect=self.connect), \
             patch.object(main, "completion", side_effect=lambda messages, max_tokens: model(messages, max_tokens)):
            main.run(claimed)
        self.db.rollback()
        versions = self.db.execute("SELECT version, content FROM transcript_versions WHERE meeting_id=%s ORDER BY version", (self.mid,)).fetchall()
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertIn("Baldibar", versions[0]["content"]["segments"][0]["text"])
        self.assertIn("Valdivar", versions[1]["content"]["segments"][0]["text"])
        self.assertEqual(versions[1]["content"]["corrections"][0]["original"], "Baldibar")
        document = self.db.execute("SELECT transcript_version FROM document_versions WHERE meeting_id=%s", (self.mid,)).fetchone()
        self.assertEqual(document["transcript_version"], 2)
        self.assertEqual(self.db.execute("SELECT state FROM meetings WHERE id=%s", (self.mid,)).fetchone()["state"], "ready")
        proposals = {r["term"]: (r["status"], r["scope"], r["folder_id"]) for r in self.db.execute("SELECT term, status, scope, folder_id FROM glossary_terms").fetchall()}
        # Proposals carry transcript examples: they stay in the meeting's folder.
        self.assertEqual(proposals.get("Atlántica"), ("propuesto", "folder", self.folder))
        undo = next(r.endpoint for r in self.api.app.routes if getattr(r, "path", "").endswith("/undo"))
        result = undo(self.mid, "c0001", (self.db, self.owner))
        self.assertEqual(result["transcript_version"], 3)
        latest = self.db.execute("SELECT content FROM transcript_versions WHERE meeting_id=%s AND version=3", (self.mid,)).fetchone()
        self.assertIn("Baldibar", latest["content"]["segments"][0]["text"])
        self.assertEqual(latest["content"]["corrections"][0]["status"], "undone")

    def run_job(self):
        from synth.worker import main, queue
        claimed = queue.claim(self.db, main.WORKER, 300)
        self.db.commit()
        model = GlossaryAwareModel([GOOD_TOPIC_1, GOOD_TOPIC_2])
        with patch.object(main, "connect", side_effect=self.connect), \
             patch.object(main, "completion", side_effect=lambda messages, max_tokens: model(messages, max_tokens)):
            main.run(claimed)
        self.db.rollback()

    def test_private_meeting_proposes_nothing_to_the_shared_glossary(self):
        self.db.execute("UPDATE meetings SET folder_id=NULL WHERE id=%s", (self.mid,))
        self.db.commit()
        self.run_job()
        terms = [r["term"] for r in self.db.execute("SELECT term FROM glossary_terms WHERE status='propuesto'").fetchall()]
        self.assertEqual(terms, [])

    def test_minutes_are_not_pinned_to_a_superseded_raw_version(self):
        from synth.contracts.transcript import Transcript
        from synth.worker.minutes_store import store_corrected
        newer = {**self.raw, "segments": [{**self.raw["segments"][0], "text": "Texto nuevo."}] + self.raw["segments"][1:]}
        newer["turns"] = [{**self.raw["turns"][0], "text": "Texto nuevo."}] + self.raw["turns"][1:]
        self.api.store_transcript_version(self.db, self.owner, self.mid, Transcript.model_validate(newer), "k2", "transcript.upload")
        self.assertIsNone(store_corrected(self.db, self.mid, 1, self.raw))
        self.assertEqual(self.db.execute("SELECT max(version) AS v FROM transcript_versions WHERE meeting_id=%s", (self.mid,)).fetchone()["v"], 2)

    def test_uploads_cannot_carry_corrections(self):
        from fastapi import HTTPException
        from synth.contracts.transcript import Transcript
        forged = {**self.raw, "corrections": [{"id": "c0001", "segment_id": "s000000", "original": "a", "replacement": "b", "term": "b"}]}
        body = self.api.TranscriptUpload(transcript=Transcript.model_validate(forged), idempotency_key="forged")
        with self.assertRaises(HTTPException) as raised:
            self.api.upload_transcript(self.mid, body, (self.db, self.owner))
        self.assertEqual(raised.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
