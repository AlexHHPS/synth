import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from synth.server import app as api
from synth.server.speakers import SpeakerAction, apply_action, display_name

ALICE = "11111111-1111-4111-8111-111111111111"


def transcript():
    return {"language": "es", "model_fingerprint": "whisper:test", "duration_ms": 9000, "segments": [
        {"id": "s000000", "text": "Hola, soy María y lo hago yo", "start_ms": 0, "end_ms": 2000, "speaker_id": "system:S1"},
        {"id": "s000001", "text": "Perfecto", "start_ms": 2500, "end_ms": 3000, "speaker_id": "microphone:local"},
        {"id": "s000002", "text": "Y yo reviso el presupuesto", "start_ms": 4000, "end_ms": 6000, "speaker_id": "system:S2"}],
        "turns": [{"id": "t000000", "text": "Hola, soy María y lo hago yo", "start_ms": 0, "end_ms": 2000,
                   "speaker_id": "system:S1", "segment_ids": ["s000000"], "word_count": 7},
                  {"id": "t000001", "text": "Perfecto", "start_ms": 2500, "end_ms": 3000,
                   "speaker_id": "microphone:local", "segment_ids": ["s000001"], "word_count": 1},
                  {"id": "t000002", "text": "Y yo reviso el presupuesto", "start_ms": 4000, "end_ms": 6000,
                   "speaker_id": "system:S2", "segment_ids": ["s000002"], "word_count": 5}]}


def act(**fields):
    return SpeakerAction(expected_version=1, **fields)


class ApplyActionTests(unittest.TestCase):
    def test_confirm_names_the_speaker_everywhere(self):
        result = apply_action(transcript(), act(action="confirm", speaker_id="system:S1", name="María", employee_id=ALICE))
        self.assertEqual(result["speakers"]["system:S1"], {"status": "confirmed", "name": "María", "employee_id": ALICE})
        self.assertEqual(result["segments"][0]["employee_id"], ALICE)
        self.assertEqual(result["turns"][0]["employee_id"], ALICE)
        self.assertEqual(display_name(result, "system:S1"), "María")

    def test_unconfirmed_speaker_is_never_named_even_when_they_say_their_name(self):
        result = transcript()
        self.assertEqual(display_name(result, "system:S1"), "Hablante 1")
        self.assertEqual(display_name(result, "system:S2"), "Hablante 2")
        self.assertEqual(display_name(result, "microphone:local"), "Tú")
        self.assertNotIn("speakers", result)

    def test_reject_keeps_the_speaker_anonymous(self):
        result = apply_action(transcript(), act(action="reject", speaker_id="system:S1"))
        self.assertEqual(result["speakers"]["system:S1"], {"status": "rejected", "name": None, "employee_id": None})
        self.assertEqual(display_name(result, "system:S1"), "Hablante 1")

    def test_rename_names_without_employee(self):
        result = apply_action(transcript(), act(action="rename", speaker_id="system:S2", name="Cliente: Ana"))
        self.assertEqual(display_name(result, "system:S2"), "Cliente: Ana")
        self.assertIsNone(result["turns"][2]["employee_id"])

    def test_merge_moves_every_row(self):
        result = apply_action(transcript(), act(action="merge", speaker_id="system:S2", into="system:S1"))
        self.assertEqual({t["speaker_id"] for t in result["turns"]}, {"system:S1", "microphone:local"})

    def test_unknown_speaker_and_bad_actions_are_rejected(self):
        with self.assertRaises(ValueError):
            apply_action(transcript(), act(action="reject", speaker_id="system:S9"))
        with self.assertRaises(ValidationError):
            act(action="confirm", speaker_id="system:S1")
        with self.assertRaises(ValidationError):
            act(action="merge", speaker_id="system:S1", into="system:S1")


class NoSessionTests(unittest.TestCase):
    def test_speaker_endpoint_requires_a_session(self):
        @contextmanager
        def fake_connect():
            yield object()
        with patch("synth.server.app.connect", fake_connect):
            response = TestClient(api.app).post(f"/v1/meetings/{uuid4()}/speakers",
                                                json={"action": "reject", "speaker_id": "system:S1", "expected_version": 1})
        self.assertEqual(response.status_code, 401)


@unittest.skipUnless(os.environ.get("SYNTH_TEST_DATABASE_URL"), "Disposable PostgreSQL URL required")
class SpeakerEndpointTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        url = os.environ["SYNTH_TEST_DATABASE_URL"]
        if "@127.0.0.1:18289/" not in url:
            self.fail("Tests require the dedicated disposable local PostgreSQL port")
        self.schema = "speakers_" + uuid4().hex
        self.admin = psycopg.connect(url, autocommit=True)
        self.admin.execute("CREATE SCHEMA " + self.schema)
        self.db = psycopg.connect(url, row_factory=dict_row, options="-c search_path=" + self.schema)
        self.db.execute((Path(__file__).resolve().parents[1] / "server/schema.sql").read_text())
        self.owner = {"id": uuid4(), "kind": "user", "name": "Owner"}
        self.other = {"id": uuid4(), "kind": "user", "name": "Other"}
        for actor in (self.owner, self.other):
            self.db.execute("INSERT INTO principals(id,name,kind) VALUES (%s,%s,%s)", (actor["id"], actor["name"], "user"))
        self.mid = api.create_meeting(api.Meeting(title="Comité"), (self.db, self.owner))["id"]
        from synth.contracts.transcript import Transcript
        api.store_transcript_version(self.db, self.owner, self.mid, Transcript.model_validate(transcript()), "k1", "transcript.upload")
        self.db.commit()
        self.route = next(r.endpoint for r in api.app.routes if getattr(r, "path", "") == "/v1/meetings/{meeting_id}/speakers")

    def tearDown(self):
        self.db.close()
        self.admin.execute("DROP SCHEMA " + self.schema + " CASCADE")
        self.admin.close()

    def test_owner_confirmation_creates_a_named_version_and_a_new_document_job(self):
        result = self.route(self.mid, act(action="confirm", speaker_id="system:S1", name="María", employee_id=ALICE),
                            (self.db, self.owner))
        self.assertEqual(result["transcript_version"], 2)
        latest = self.db.execute("SELECT content FROM transcript_versions WHERE meeting_id=%s AND version=2", (self.mid,)).fetchone()
        self.assertEqual(latest["content"]["speakers"]["system:S1"]["name"], "María")
        jobs = self.db.execute("SELECT payload FROM jobs WHERE meeting_id=%s AND kind='document' ORDER BY created_at", (self.mid,)).fetchall()
        self.assertEqual([j["payload"]["transcript_version"] for j in jobs], [1, 2])

    def test_only_the_owner_may_act(self):
        with self.assertRaises(HTTPException) as raised:
            self.route(self.mid, act(action="reject", speaker_id="system:S1"), (self.db, self.other))
        self.assertEqual(raised.exception.status_code, 404)

    def test_stale_version_conflicts(self):
        with self.assertRaises(HTTPException) as raised:
            self.route(self.mid, SpeakerAction(action="reject", speaker_id="system:S1", expected_version=7), (self.db, self.owner))
        self.assertEqual(raised.exception.status_code, 409)

    def test_batch_carries_human_names_in_one_version_and_retries_after_a_lost_response(self):
        from synth.server.speakers import SpeakerBatch
        route = next(r.endpoint for r in api.app.routes if getattr(r,'path','') == '/v1/meetings/{meeting_id}/speakers/batch')
        body = SpeakerBatch(actions=[act(action='confirm',speaker_id='system:S1',name='María',employee_id=ALICE),
                                     act(action='confirm',speaker_id='system:S2',name='Ana')])
        result = route(self.mid,body,(self.db,self.owner))
        self.assertEqual(result['transcript_version'],2)
        repeated = route(self.mid,body,(self.db,self.owner))
        self.assertTrue(repeated['reused'])
        self.assertEqual(repeated['job_id'],result['job_id'])
        latest = self.db.execute('SELECT content FROM transcript_versions WHERE meeting_id=%s AND version=2',(self.mid,)).fetchone()['content']
        self.assertEqual(latest['speakers']['system:S1']['name'],'María')
        self.assertEqual(latest['speakers']['system:S2']['name'],'Ana')
        with self.assertRaises(HTTPException) as denied:
            route(self.mid,body,(self.db,self.other))
        self.assertEqual(denied.exception.status_code,404)



class DocumentPromptNamesTests(unittest.TestCase):
    """the text model only ever sees resolved names."""

    def prompt_for(self, content):
        from synth.worker import document
        seen = []

        def fake_completion(messages, **kwargs):
            seen.append(messages[1]["content"])
            raise ValueError("stop_after_prompt")
        with patch.object(document, "completion", fake_completion):
            with self.assertRaises(ValueError):
                document.generate(content, "")
        return seen[0]

    def test_unconfirmed_speaker_is_hablante_even_when_saying_a_name(self):
        prompt = self.prompt_for(transcript())
        self.assertIn('"speaker": "Hablante 1"', prompt)
        self.assertIn('"speaker": "Tú"', prompt)
        self.assertNotIn("system:S1", prompt)
        self.assertNotIn('"turns"', prompt)

    def test_confirmed_name_reaches_the_prompt(self):
        named = apply_action(transcript(), act(action="confirm", speaker_id="system:S1", name="María", employee_id=ALICE))
        prompt = self.prompt_for(named)
        self.assertIn('"speaker": "María"', prompt)
        self.assertNotIn(ALICE, prompt)


class UploadCannotNameTests(SpeakerEndpointTests):
    def test_upload_with_names_or_employee_ids_is_refused(self):
        from synth.contracts.transcript import Transcript
        named = apply_action(transcript(), act(action="confirm", speaker_id="system:S1", name="CEO", employee_id=ALICE))
        body = api.TranscriptUpload(transcript=Transcript.model_validate(named), idempotency_key="k-named")
        with self.assertRaises(HTTPException) as raised:
            api.upload_transcript(self.mid, body, (self.db, self.owner))
        self.assertEqual(raised.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
