"""Questions to the meetings with citations and folder permissions; follow-up email draft; MCP."""
import datetime
import json
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from synth.server.follow_up import compose
from synth.worker import answers

PASSAGES = [
    {"meeting_id": "m1", "transcript_version": 1, "meeting_title": "Comité Valdivar", "meeting_date": "2026-10-20",
     "turn_id": "t000002", "time": "00:10", "speaker": "María", "text": "Decidimos usar Nimbus porque Lucerno exige datos en la UE.", "before": ""},
    {"meeting_id": "m1", "transcript_version": 1, "meeting_title": "Comité Valdivar", "meeting_date": "2026-10-20",
     "turn_id": "t000003", "time": "00:15", "speaker": "Ana", "text": "Revisaré los permisos de las carpetas.", "before": ""},
]


def model(body):
    calls = []

    def complete(messages, max_tokens):
        calls.append(messages)
        return {"content": json.dumps(body, ensure_ascii=False), "effective_model": "fake"}
    complete.calls = calls
    return complete


class AnswerTests(unittest.TestCase):
    def test_answer_keeps_only_citations_of_retrieved_passages(self):
        result = answers.answer("¿Por qué Nimbus?", PASSAGES, model({"answer": "Porque Lucerno exige datos en la UE.",
                                                                      "citations": ["p1", "p9", "t000003"], "consta": True}))
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["citations"], [{"meeting_id": "m1", "turn_id": "t000002", "transcript_version": 1}])

    def test_answer_without_valid_citation_is_no_consta(self):
        for body in ({"answer": "Por el precio.", "citations": [], "consta": True},
                     {"answer": "Por el precio.", "citations": ["p7"], "consta": True},
                     {"answer": "No se dice.", "citations": ["p1"], "consta": False}):
            with self.subTest(body=body):
                result = answers.answer("¿Por qué Nimbus?", PASSAGES, model(body))
                self.assertEqual((result["status"], result["citations"]), ("no_consta", []))
                self.assertNotIn("precio", result["text"])

    def test_no_passages_means_no_model_call(self):
        complete = model({"answer": "inventado", "citations": ["p1"]})
        self.assertEqual(answers.answer("¿Qué presupuesto?", [], complete)["status"], "no_consta")
        self.assertEqual(complete.calls, [])

    def test_passages_are_marked_as_untrusted_content(self):
        complete = model({"answer": "x", "citations": ["p1"]})
        answers.answer("¿Por qué Nimbus?", PASSAGES, complete)
        self.assertIn("no confiable", complete.calls[0][0]["content"])
        self.assertIn("[p1] Comité Valdivar", complete.calls[0][1]["content"])


class FollowUpEmailTests(unittest.TestCase):
    MINUTES = {"tldr": "El piloto de Valdivar pasa al 15 de noviembre.",
               "decisions": [{"decision": "Usar Nimbus", "reason": "datos en la UE", "status": "acordada"},
                             {"decision": "Cambiar de proveedor", "reason": None, "status": "aparcada"}],
               "tasks": [{"title": "Preparar el piloto", "owner": "Tú", "due_date": "2026-11-14", "due_text": "el viernes", "status": "abierta"},
                         {"title": "Revisar permisos", "owner": "sin asignar", "due_date": None, "due_text": None, "status": "abierta"},
                         {"title": "Tarea cerrada", "owner": "Ana", "due_date": None, "due_text": None, "status": "hecha"}],
               "open_questions": [{"text": "Plazo de retención del audio."}]}

    def test_draft_says_what_the_minutes_say(self):
        draft = compose("Comité Valdivar", datetime.datetime(2026, 10, 27, 10), self.MINUTES)
        self.assertEqual(draft["subject"], "Resumen y próximos pasos: Comité Valdivar")
        body = draft["body"]
        self.assertIn("«Comité Valdivar» del 27 de octubre", body)
        self.assertIn("- Usar Nimbus (motivo: datos en la UE)", body)
        self.assertIn("- Preparar el piloto — Tú, 14 de noviembre", body)
        self.assertIn("- Revisar permisos — sin responsable", body)
        self.assertIn("- Plazo de retención del audio.", body)
        self.assertNotIn("Cambiar de proveedor", body)
        self.assertNotIn("Tarea cerrada", body)

    def test_the_product_has_no_way_to_send_mail(self):
        from synth.server import app as api
        paths = {getattr(r, "path", "") for r in api.app.routes}
        self.assertIn("/v1/meetings/{meeting_id}/follow-up-email", paths)
        self.assertFalse([p for p in paths if "send" in p or "mail" in p and p != "/v1/meetings/{meeting_id}/follow-up-email"])
        source = (Path(__file__).resolve().parents[1] / "server/follow_up.py").read_text()
        for marker in ("smtplib", "sendmail", "gmail", "recipients"):
            self.assertNotIn(marker, source)


TURNS = [("system:S1", "Decidimos usar Nimbus porque Lucerno exige que los datos se queden en la Unión Europea."),
         ("system:S2", "Ana revisará los permisos de las carpetas antes del viernes."),
         ("microphone:local", "El presupuesto del piloto son 40.000 euros.")]


def transcript(turns):
    return {"language": "es", "model_fingerprint": "whisper:test", "duration_ms": 60000,
            "segments": [{"id": f"s{i:06d}", "text": text, "start_ms": i * 5000, "end_ms": i * 5000 + 4000, "speaker_id": speaker}
                         for i, (speaker, text) in enumerate(turns)],
            "turns": [{"id": f"t{i:06d}", "text": text, "start_ms": i * 5000, "end_ms": i * 5000 + 4000, "speaker_id": speaker,
                       "segment_ids": [f"s{i:06d}"], "word_count": len(text.split())} for i, (speaker, text) in enumerate(turns)]}


@unittest.skipUnless(os.environ.get("SYNTH_TEST_DATABASE_URL"), "Disposable PostgreSQL URL required")
class QuestionFlowTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from synth.contracts.transcript import Transcript
        from synth.server import app as api
        self.api = api
        url = os.environ["SYNTH_TEST_DATABASE_URL"]
        if "@127.0.0.1:18289/" not in url:
            self.fail("Tests require the dedicated disposable local PostgreSQL port")
        self.url, self.schema = url, "questions_" + uuid4().hex
        self.admin = psycopg.connect(url, autocommit=True)
        self.admin.execute("CREATE SCHEMA " + self.schema)
        self.db = self.connect()
        self.db.execute((Path(__file__).resolve().parents[1] / "server/schema.sql").read_text())
        self.alice = {"id": uuid4(), "kind": "user", "name": "Alice"}
        self.bob = {"id": uuid4(), "kind": "user", "name": "Bob"}
        for actor in (self.alice, self.bob):
            self.db.execute("INSERT INTO principals(id,name,kind) VALUES (%s,%s,%s)", (actor["id"], actor["name"], "user"))
        self.folder = uuid4()
        self.db.execute("INSERT INTO folders(id,owner_id,name) VALUES (%s,%s,%s)", (self.folder, self.alice["id"], "Clientes"))
        self.shared = api.create_meeting(api.Meeting(title="Comité Valdivar", folder_id=self.folder), (self.db, self.alice))["id"]
        self.private = api.create_meeting(api.Meeting(title="Mi 1:1"), (self.db, self.alice))["id"]
        api.store_transcript_version(self.db, self.alice, self.shared, Transcript.model_validate(transcript(TURNS)), "k1", "transcript.upload")
        api.store_transcript_version(self.db, self.alice, self.private, Transcript.model_validate(
            transcript([("microphone:local", "Nimbus me parece caro para el piloto personal.")])), "k2", "transcript.upload")
        self.db.commit()

    def connect(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.url, row_factory=dict_row, options="-c search_path=" + self.schema)

    def tearDown(self):
        self.db.close()
        self.admin.execute("DROP SCHEMA " + self.schema + " CASCADE")
        self.admin.close()

    def route(self, method, path):
        return next(r.endpoint for r in self.api.app.routes if getattr(r, "path", "") == path and method in r.methods)

    def ask(self, actor, question, **scope):
        from synth.server.questions import Ask
        return self.route("POST", "/v1/questions")(Ask(question=question, **scope), (self.db, actor))

    def test_retrieval_only_reads_meetings_the_asker_may_read(self):
        from synth.server.questions import retrieve
        alice = retrieve(self.db, self.alice, "¿Por qué elegimos Nimbus?")
        self.assertEqual({p["meeting_id"] for p in alice}, {str(self.shared), str(self.private)})
        self.assertEqual(retrieve(self.db, self.bob, "¿Por qué elegimos Nimbus?"), [])
        self.db.execute("INSERT INTO folder_memberships(folder_id,principal_id,role) VALUES (%s,%s,'reader')", (self.folder, self.bob["id"]))
        bob = retrieve(self.db, self.bob, "¿Por qué elegimos Nimbus?")
        self.assertEqual({p["meeting_id"] for p in bob}, {str(self.shared)})
        self.assertEqual(bob[0]["turn_id"], "t000000")
        self.assertIn("Unión Europea", bob[0]["text"])

    def test_a_question_about_a_meeting_one_cannot_read_is_refused_before_anything_is_stored(self):
        from fastapi import HTTPException
        for scope in ({"meeting_id": self.private}, {"meeting_id": uuid4()}, {"folder_id": self.folder}, {"folder_id": uuid4()}):
            with self.assertRaises(HTTPException) as refused:  # «¿Qué?» has no search words: retrieval never runs
                self.ask(self.bob, "¿Qué?", **scope)
            self.assertEqual(refused.exception.status_code, 404, scope)
        self.assertEqual(self.db.execute("SELECT count(*) AS n FROM questions").fetchone()["n"], 0)

    def test_without_evidence_the_answer_is_no_consta_and_no_model_runs(self):
        asked = self.ask(self.alice, "¿Qué dijo el auditor externo sobre la nómina?")
        self.assertEqual(asked["state"], "answered")
        self.assertEqual(asked["answer"]["status"], "no_consta")
        self.assertEqual(self.db.execute("SELECT passages FROM questions WHERE id=%s", (asked["id"],)).fetchone()["passages"], [])

    def test_worker_answers_with_citations_and_only_the_asker_reads_it(self):
        from fastapi import HTTPException
        asked = self.ask(self.alice, "¿Por qué Nimbus?", folder_id=self.folder)
        self.assertEqual(asked["state"], "queued")
        self.db.commit()
        complete = model({"answer": "Porque Lucerno exige que los datos se queden en la UE.", "citations": ["p1"], "consta": True})
        self.assertTrue(answers.step(self.connect, complete))
        self.assertNotIn("1:1", complete.calls[0][1]["content"])  # the private meeting is outside the folder scope
        result = self.route("GET", "/v1/questions/{question_id}")(asked["id"], (self.db, self.alice))
        self.assertEqual(result["answer"]["status"], "answered")
        self.assertEqual(result["answer"]["citations"][0]["turn_id"], "t000000")
        self.assertEqual(result["answer"]["citations"][0]["meeting_title"], "Comité Valdivar")
        with self.assertRaises(HTTPException) as raised:
            self.route("GET", "/v1/questions/{question_id}")(asked["id"], (self.db, self.bob))
        self.assertEqual(raised.exception.status_code, 404)

    def test_losing_access_hides_an_old_answer(self):
        from fastapi import HTTPException
        self.db.execute("INSERT INTO folder_memberships(folder_id,principal_id,role) VALUES (%s,%s,'reader')", (self.folder, self.bob["id"]))
        asked = self.ask(self.bob, "¿Por qué Nimbus?")
        self.db.execute("DELETE FROM folder_memberships WHERE principal_id=%s", (self.bob["id"],))
        with self.assertRaises(HTTPException) as raised:
            self.route("GET", "/v1/questions/{question_id}")(asked["id"], (self.db, self.bob))
        self.assertEqual(raised.exception.status_code, 404)

    def test_failed_model_retries_then_reports(self):
        self.ask(self.alice, "¿Por qué Nimbus?")
        self.db.commit()

        def broken(messages, max_tokens):
            raise ValueError("gateway_unavailable_or_invalid")
        for _ in range(answers.MAX_ATTEMPTS):
            self.assertTrue(answers.step(self.connect, broken))
            self.db.execute("UPDATE questions SET lease_until=now()-interval '1 second' WHERE state='running'")
            self.db.commit()
        row = self.db.execute("SELECT state, error_code, answer FROM questions").fetchone()
        self.assertEqual((row["state"], row["error_code"], row["answer"]), ("failed", "gateway_unavailable_or_invalid", None))
        self.assertFalse(answers.step(self.connect, broken))

    def test_a_question_abandoned_on_its_last_attempt_is_reported(self):
        asked = self.ask(self.alice, "¿Por qué Nimbus?")
        self.db.execute("UPDATE questions SET state='running', attempts=%s, lease_until=now()-interval '1 second' WHERE id=%s",
                        (answers.MAX_ATTEMPTS, asked["id"]))
        self.db.commit()
        self.assertFalse(answers.step(self.connect, model({"answer": "x", "citations": ["p1"]})))
        row = self.db.execute("SELECT state, error_code FROM questions WHERE id=%s", (asked["id"],)).fetchone()
        self.assertEqual((row["state"], row["error_code"]), ("failed", "answer_abandoned"))

    def test_mcp_ask_meetings_respects_key_folder_scope(self):
        from fastapi import Request
        key = uuid4()
        machine = {"id": uuid4(), "kind": "machine", "name": "MCP", "key_id": key}
        self.db.execute("INSERT INTO principals(id,name,kind) VALUES (%s,%s,%s)", (machine["id"], "MCP", "machine"))
        rpc = self.api.RPC(jsonrpc="2.0", id=1, method="tools/call", params={"name": "ask_meetings", "arguments": {"question": "¿Por qué Nimbus?"}})
        request = Request({"type": "http", "headers": []})
        result = self.api.mcp(rpc, (self.db, machine), request)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["answer"]["status"], "no_consta")
        self.db.execute("INSERT INTO folder_memberships(folder_id,principal_id,role) VALUES (%s,%s,'reader')", (self.folder, machine["id"]))
        self.db.execute("INSERT INTO access_keys(id,principal_id,token_hash) VALUES (%s,%s,%s)", (key, machine["id"], uuid4().hex))
        self.db.execute("INSERT INTO key_folder_scopes(key_id,folder_id) VALUES (%s,%s)", (key, self.folder))
        with patch.object(self.api.questions, "wait", side_effect=lambda factory, actor, qid, seconds=45: self.api.questions.read(self.db, actor, qid)):
            queued = json.loads(self.api.mcp(rpc, (self.db, machine), request)["result"]["content"][0]["text"])
        self.assertEqual(queued["state"], "queued")
        passages = self.db.execute("SELECT passages FROM questions WHERE id=%s", (queued["id"],)).fetchone()["passages"]
        self.assertEqual({p["meeting_id"] for p in passages}, {str(self.shared)})
        listed = {t["name"] for t in self.api.mcp(self.api.RPC(jsonrpc="2.0", id=2, method="tools/list", params={}), (self.db, machine), request)["result"]["tools"]}
        self.assertTrue({"ask_meetings", "get_answer"} <= listed)

    def test_follow_up_email_endpoint_needs_access_and_minutes(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as raised:
            self.route("GET", "/v1/meetings/{meeting_id}/follow-up-email")(self.shared, (self.db, self.bob))
        self.assertEqual(raised.exception.status_code, 404)
        with self.assertRaises(HTTPException) as raised:
            self.route("GET", "/v1/meetings/{meeting_id}/follow-up-email")(self.shared, (self.db, self.alice))
        self.assertEqual(raised.exception.status_code, 404)


class QuestionsNoSessionTests(unittest.TestCase):
    def test_every_new_endpoint_requires_a_session(self):
        from fastapi.testclient import TestClient
        from synth.server import app as api

        @contextmanager
        def fake_connect():
            yield object()
        meeting, question = uuid4(), uuid4()
        calls = [("POST", "/v1/questions"), ("GET", f"/v1/questions/{question}"), ("GET", f"/v1/meetings/{meeting}/follow-up-email")]
        with patch("synth.server.app.connect", fake_connect):
            client = TestClient(api.app)
            for method, path in calls:
                with self.subTest(path=path):
                    self.assertEqual(client.request(method, path, json={"question": "¿Por qué Nimbus?"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
