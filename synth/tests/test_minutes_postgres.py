"""Minutes v2 through the real worker against a disposable PostgreSQL: context, publication, tasks/decisions."""
import json
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from synth.tests.test_minutes import GOOD_TOPIC_1, GOOD_TOPIC_2, TRANSCRIPT, FakeModel


@unittest.skipUnless(os.environ.get("SYNTH_TEST_DATABASE_URL"), "Disposable PostgreSQL URL required")
class WorkerMinutesTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        from synth.server import app as api
        self.api = api
        url = os.environ["SYNTH_TEST_DATABASE_URL"]
        if "@127.0.0.1:18289/" not in url:
            self.fail("Tests require the dedicated disposable local PostgreSQL port")
        self.url, self.schema = url, "minutes_" + uuid4().hex
        self.admin = psycopg.connect(url, autocommit=True)
        self.admin.execute("CREATE SCHEMA " + self.schema)
        self.db = self.connect()
        self.db.execute((Path(__file__).resolve().parents[1] / "server/schema.sql").read_text())
        self.owner = {"id": uuid4(), "kind": "user", "name": "Owner"}
        self.db.execute("INSERT INTO principals(id,name,kind) VALUES (%s,%s,%s)", (self.owner["id"], "Owner", "user"))
        context = (self.db, self.owner)
        folder = api.create_folder(api.Name(name="Clientes"), context)["id"] if hasattr(api, "create_folder") else None
        self.mid = api.create_meeting(api.Meeting(title="Comité Valdivar", folder_id=folder), context)["id"]
        self.db.execute("INSERT INTO glossary_terms(id,scope,term,status,definition) VALUES (%s,'workspace','Valdivar','aprobado','cliente industrial')", (uuid4(),))
        from synth.contracts.transcript import Transcript
        plain = {k: v for k, v in TRANSCRIPT.items() if k != "speakers"}
        api.store_transcript_version(self.db, self.owner, self.mid, Transcript.model_validate(plain), "k1", "transcript.upload")
        self.db.commit()

    def connect(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.url, row_factory=dict_row, options="-c search_path=" + self.schema)

    def tearDown(self):
        self.db.close()
        self.admin.execute("DROP SCHEMA " + self.schema + " CASCADE")
        self.admin.close()

    def test_worker_publishes_v2_minutes_with_tasks_and_decisions(self):
        from synth.worker import main, queue
        claimed = queue.claim(self.db, main.WORKER, 300)
        self.db.commit()
        model = FakeModel([GOOD_TOPIC_1, GOOD_TOPIC_2])
        with patch.object(main, "connect", side_effect=self.connect), \
             patch.object(main, "completion", side_effect=lambda messages, max_tokens, on_delta=None: model(messages, max_tokens)):
            main.run(claimed)
        self.db.rollback()
        document = self.db.execute("SELECT content, markdown FROM document_versions WHERE meeting_id=%s", (self.mid,)).fetchone()
        self.assertEqual(document["content"]["schema_version"], 2)
        tasks = self.db.execute("SELECT title, owner_name, owner_principal_id, status FROM tasks WHERE meeting_id=%s ORDER BY item_key", (self.mid,)).fetchall()
        self.assertEqual([t["owner_name"] for t in tasks], ["Tú", "Ana"])
        self.assertEqual(tasks[0]["owner_principal_id"], self.owner["id"])
        decisions = self.db.execute("SELECT decision, reason FROM decisions WHERE meeting_id=%s", (self.mid,)).fetchall()
        self.assertEqual(decisions[0]["reason"], "el cliente quiere datos en la UE")
        header = model.calls[0][1]["content"]
        self.assertIn("Valdivar (concepto: cliente industrial)", header)
        self.assertIn("Título: Comité Valdivar", header)

    def publish(self):
        from synth.worker import main, queue
        claimed = queue.claim(self.db, main.WORKER, 300)
        self.db.commit()
        model = FakeModel([GOOD_TOPIC_1, GOOD_TOPIC_2])
        with patch.object(main, "connect", side_effect=self.connect), \
             patch.object(main, "completion", side_effect=lambda messages, max_tokens, on_delta=None: model(messages, max_tokens)):
            main.run(claimed)
        self.db.rollback()

    def test_folder_move_while_model_is_running_cannot_publish_or_checkpoint_old_context(self):
        self.context_change_during_inference('UPDATE meetings SET folder_id=NULL WHERE id=%s',(self.mid,))

    def test_glossary_revocation_while_model_is_running_rewrites_the_acta_with_the_current_glossary(self):
        """0.6.0: a glossary edit no longer fails the acta. The answer written with the revoked term is
        neither saved nor published; the acta is written again with the glossary as it now is."""
        from synth.worker import main,queue
        claimed=queue.claim(self.db,main.WORKER,300);self.db.commit()
        model=FakeModel([GOOD_TOPIC_1,GOOD_TOPIC_2,GOOD_TOPIC_1,GOOD_TOPIC_2])
        def completion(messages,max_tokens,on_delta=None):
            with self.connect() as changed:
                changed.execute("UPDATE glossary_terms SET status='descartado' WHERE term=%s",('Valdivar',))
            return model(messages,max_tokens)
        with patch.object(main,'connect',side_effect=self.connect),patch.object(main,'completion',side_effect=completion):
            main.run(claimed)
        self.db.rollback()
        self.assertEqual(len(model.calls),2)
        self.assertIn('Valdivar (concepto',model.calls[0][1]['content'])
        self.assertNotIn('Valdivar (concepto',model.calls[1][1]['content'])
        self.assertEqual(self.db.execute('SELECT state FROM jobs WHERE id=%s',(claimed['id'],)).fetchone()['state'],'succeeded')
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM document_versions WHERE meeting_id=%s',(self.mid,)).fetchone()['n'],1)
        # Only the answer written with the current glossary was kept.
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM document_model_responses WHERE job_id=%s',(claimed['id'],)).fetchone()['n'],1)

    def test_another_meeting_adding_glossary_terms_does_not_interrupt_a_valid_generation(self):
        from synth.worker import main,queue
        claimed=queue.claim(self.db,main.WORKER,300);self.db.commit()
        model=FakeModel([GOOD_TOPIC_1,GOOD_TOPIC_2]);added=False
        def completion(messages, max_tokens, on_delta=None):
            nonlocal added
            if not added:
                with self.connect() as updated:
                    updated.execute("INSERT INTO glossary_terms(id,scope,term,status) VALUES (%s,'workspace','Nuevo término de otra reunión','aprobado')",(uuid4(),))
                added=True
            return model(messages,max_tokens)
        with patch.object(main,'connect',side_effect=self.connect),patch.object(main,'completion',side_effect=completion):
            main.run(claimed)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT state FROM jobs WHERE id=%s',(claimed['id'],)).fetchone()['state'],'succeeded')

    def context_change_during_inference(self,statement,parameters):
        from synth.worker import main,queue
        claimed=queue.claim(self.db,main.WORKER,300);self.db.commit()
        model=FakeModel([GOOD_TOPIC_1,GOOD_TOPIC_2])
        calls=[]
        def completion(messages, max_tokens, on_delta=None):
            calls.append(messages)
            with self.connect() as moved:
                moved.execute(statement,parameters)
            return model(messages,max_tokens)
        with patch.object(main,'connect',side_effect=self.connect),patch.object(main,'completion',side_effect=completion):
            main.run(claimed)
        self.db.rollback()
        self.assertEqual(len(calls),1)
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM document_versions WHERE meeting_id=%s',(self.mid,)).fetchone()['n'],0)
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM document_model_responses WHERE job_id=%s',(claimed['id'],)).fetchone()['n'],0)
        # 0.6.0: not a dead end. The job waits and runs again with the context as it now is.
        job=self.db.execute("SELECT state,error_code,available_at>now() AS later FROM jobs WHERE id=%s",(claimed['id'],)).fetchone()
        self.assertEqual(job,{'state':'queued','error_code':'generation_context_changed','later':True})
        generation=self.db.execute('SELECT state FROM document_generations WHERE meeting_id=%s',(self.mid,)).fetchone()
        self.assertEqual(generation['state'],'superseded')
        self.assertEqual(self.db.execute('SELECT state FROM meetings WHERE id=%s',(self.mid,)).fetchone()['state'],'processing')

    def test_a_failed_attempt_runs_again_by_itself_and_publishes(self):
        from synth.worker import main,queue
        claimed=queue.claim(self.db,main.WORKER,300);self.db.commit()
        def down(messages,max_tokens,on_delta=None):
            raise ValueError('gateway_unavailable_or_invalid')
        with patch.object(main,'connect',side_effect=self.connect),patch.object(main,'completion',side_effect=down):
            main.run(claimed)
        self.db.rollback()
        self.assertIsNone(queue.claim(self.db,main.WORKER,300))  # it waits before trying again
        self.db.execute("UPDATE jobs SET available_at=now() WHERE id=%s",(claimed['id'],));self.db.commit()
        again=queue.claim(self.db,main.WORKER,300);self.db.commit()
        self.assertEqual(again['attempts'],2)
        model=FakeModel([GOOD_TOPIC_1,GOOD_TOPIC_2])
        with patch.object(main,'connect',side_effect=self.connect),patch.object(main,'completion',side_effect=lambda messages,max_tokens,on_delta=None:model(messages,max_tokens)):
            main.run(again)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT state FROM jobs WHERE id=%s',(claimed['id'],)).fetchone()['state'],'succeeded')
        self.assertEqual(self.db.execute('SELECT state FROM meetings WHERE id=%s',(self.mid,)).fetchone()['state'],'ready')

    def test_after_automatic_attempts_run_out_the_meeting_asks_for_attention(self):
        from synth.worker import main,queue
        self.db.execute("UPDATE jobs SET attempts=%s",(len(queue.RETRY_DELAYS_S),));self.db.commit()
        claimed=queue.claim(self.db,main.WORKER,300);self.db.commit()
        def down(messages,max_tokens,on_delta=None):
            raise ValueError('gateway_unavailable_or_invalid')
        with patch.object(main,'connect',side_effect=self.connect),patch.object(main,'completion',side_effect=down):
            main.run(claimed)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT state FROM jobs WHERE id=%s',(claimed['id'],)).fetchone()['state'],'failed')
        self.assertEqual(self.db.execute('SELECT state FROM meetings WHERE id=%s',(self.mid,)).fetchone()['state'],'failed')

    def test_a_late_attempt_writes_the_acta_with_the_sectioned_pipeline(self):
        from synth.worker import main,queue
        self.db.execute("UPDATE jobs SET attempts=%s",(main.SECTIONED_FROM_ATTEMPT-1,));self.db.commit()
        claimed=queue.claim(self.db,main.WORKER,300);self.db.commit()
        model=FakeModel([GOOD_TOPIC_1,GOOD_TOPIC_2])
        with patch.object(main,'connect',side_effect=self.connect),patch.object(main,'completion',side_effect=lambda messages,max_tokens,on_delta=None:model(messages,max_tokens)):
            main.run(claimed)
        self.db.rollback()
        self.assertTrue(any('Divide este tramo' in call[1]['content'] for call in model.calls))
        self.assertEqual(self.db.execute('SELECT state FROM jobs WHERE id=%s',(claimed['id'],)).fetchone()['state'],'succeeded')

    def resume_interrupted_context(self,move):
        from synth.worker import main,queue
        class ProcessExit(BaseException):
            pass
        claimed=queue.claim(self.db,main.WORKER,300);self.db.commit()
        first=FakeModel([GOOD_TOPIC_1,GOOD_TOPIC_2])
        def interrupted(messages, max_tokens, on_delta=None):
            if 'Tema: Infraestructura y permisos\n\nExtrae' in messages[1]['content']:
                raise ProcessExit()
            return first(messages,max_tokens)
        # Per-topic resumption belongs to the sectioned pipeline, kept for rollback.
        sectioned=patch.object(main,'STRATEGY','sectioned');sectioned.start();self.addCleanup(sectioned.stop)
        with patch.object(main,'connect',side_effect=self.connect),patch.object(main,'completion',side_effect=interrupted):
            with self.assertRaises(ProcessExit):
                main.run(claimed)
        self.db.rollback()
        saved=self.db.execute('SELECT checkpoint FROM jobs WHERE id=%s',(claimed['id'],)).fetchone()['checkpoint']
        self.assertEqual(len(saved['minutes']['topics']),1)
        if move:
            self.db.execute('UPDATE meetings SET folder_id=NULL WHERE id=%s',(self.mid,))
        self.db.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s",(claimed['id'],))
        self.db.commit()
        recovered=queue.claim(self.db,main.WORKER,300);self.db.commit()
        model=FakeModel([GOOD_TOPIC_1,GOOD_TOPIC_2] if move else [GOOD_TOPIC_2])
        with patch.object(main,'connect',side_effect=self.connect),patch.object(main,'completion',side_effect=lambda messages, max_tokens, on_delta=None:model(messages,max_tokens)):
            main.run(recovered)
        self.db.rollback()
        result=self.db.execute('SELECT state,checkpoint FROM jobs WHERE id=%s',(claimed['id'],)).fetchone()
        self.assertEqual(result['state'],'succeeded')
        if move:
            self.assertNotEqual(saved['context_binding'],result['checkpoint']['context_binding'])
            self.assertIn('Divide este tramo',model.calls[0][1]['content'])
            self.assertNotIn('Carpeta:',model.calls[0][1]['content'])
        else:
            self.assertEqual(saved['context_binding'],result['checkpoint']['context_binding'])
            self.assertIn('Tema: Infraestructura y permisos',model.calls[0][1]['content'])
        self.assertEqual(self.db.execute('SELECT count(*) AS n FROM document_versions WHERE meeting_id=%s',(self.mid,)).fetchone()['n'],1)

    def test_restart_reuses_accepted_work_when_context_has_not_changed(self):
        self.resume_interrupted_context(False)

    def test_restart_after_folder_move_rebuilds_work_with_current_context(self):
        self.resume_interrupted_context(True)

    def route(self, method, path):
        return next(r.endpoint for r in self.api.app.routes if getattr(r, "path", "") == path and method in r.methods)

    def test_tasks_decisions_lists_edits_and_my_tasks(self):
        from fastapi import HTTPException
        self.publish()
        context = (self.db, self.owner)
        tasks = self.route("GET", "/v1/meetings/{meeting_id}/tasks")(self.mid, context)["items"]
        self.assertEqual([t["title"] for t in tasks], ["Preparar el piloto con 40.000 euros", "Revisar los permisos de las carpetas"])
        mine = self.route("GET", "/v1/tasks")(context)["items"]
        self.assertEqual([t["title"] for t in mine], ["Preparar el piloto con 40.000 euros"])
        from synth.server.items import TaskUpdate, DecisionUpdate
        done = self.route("PATCH", "/v1/tasks/{task_id}")(tasks[0]["id"], TaskUpdate(status="hecha"), context)
        self.assertEqual(done["status"], "hecha")
        self.assertEqual(self.route("GET", "/v1/tasks")(context)["items"], [])
        decisions = self.route("GET", "/v1/meetings/{meeting_id}/decisions")(self.mid, context)["items"]
        parked = self.route("PATCH", "/v1/decisions/{decision_id}")(decisions[0]["id"], DecisionUpdate(status="aparcada"), context)
        self.assertEqual(parked["status"], "aparcada")
        stranger = {"id": uuid4(), "kind": "user", "name": "Stranger"}
        self.db.execute("INSERT INTO principals(id,name,kind) VALUES (%s,%s,%s)", (stranger["id"], "Stranger", "user"))
        with self.assertRaises(HTTPException) as raised:
            self.route("PATCH", "/v1/tasks/{task_id}")(tasks[1]["id"], TaskUpdate(status="hecha"), (self.db, stranger))
        self.assertEqual(raised.exception.status_code, 404)

    def test_integrations_cannot_edit_tasks_decisions_or_regenerate(self):
        from fastapi import HTTPException
        from synth.server.items import DecisionUpdate, TaskUpdate
        self.publish()
        context = (self.db, self.owner)
        task = self.route("GET", "/v1/meetings/{meeting_id}/tasks")(self.mid, context)["items"][0]
        decision = self.route("GET", "/v1/meetings/{meeting_id}/decisions")(self.mid, context)["items"][0]
        machine = {"id": uuid4(), "kind": "machine", "name": "MCP", "key_id": uuid4()}
        for call in (lambda: self.route("PATCH", "/v1/tasks/{task_id}")(task["id"], TaskUpdate(status="hecha"), (self.db, machine)),
                     lambda: self.route("PATCH", "/v1/decisions/{decision_id}")(decision["id"], DecisionUpdate(status="aparcada"), (self.db, machine)),
                     lambda: self.route("POST", "/v1/meetings/{meeting_id}/regenerate")(self.mid, (self.db, machine))):
            with self.assertRaises(HTTPException) as raised:
                call()
            self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(self.route("GET", "/v1/meetings/{meeting_id}/tasks")(self.mid, context)["items"][0]["status"], "abierta")

    def test_regenerated_minutes_keep_what_a_person_edited(self):
        from synth.server.items import TaskUpdate
        from synth.worker import main, queue
        self.publish()
        context = (self.db, self.owner)
        tasks = self.route("GET", "/v1/meetings/{meeting_id}/tasks")(self.mid, context)["items"]
        self.route("PATCH", "/v1/tasks/{task_id}")(tasks[1]["id"], TaskUpdate(status="descartada"), context)
        self.route("POST", "/v1/meetings/{meeting_id}/regenerate")(self.mid, context)
        self.db.commit()
        claimed = queue.claim(self.db, main.WORKER, 300)
        self.db.commit()
        model = FakeModel([GOOD_TOPIC_1, GOOD_TOPIC_2])
        with patch.object(main, "connect", side_effect=self.connect), \
             patch.object(main, "completion", side_effect=lambda messages, max_tokens, on_delta=None: model(messages, max_tokens)):
            main.run(claimed)
        self.db.rollback()
        latest = self.route("GET", "/v1/meetings/{meeting_id}/tasks")(self.mid, context)["items"]
        self.assertNotEqual(latest[1]["id"], tasks[1]["id"])
        self.assertEqual(latest[1]["status"], "descartada")
        self.assertEqual(latest[0]["status"], "abierta")

    def test_regenerate_queues_new_minutes_once(self):
        from fastapi import HTTPException
        self.publish()
        context = (self.db, self.owner)
        result = self.route("POST", "/v1/meetings/{meeting_id}/regenerate")(self.mid, context)
        self.assertEqual(result["transcript_version"], 1)
        with self.assertRaises(HTTPException) as raised:
            self.route("POST", "/v1/meetings/{meeting_id}/regenerate")(self.mid, context)
        self.assertEqual(raised.exception.status_code, 409)


class ItemsNoSessionTests(unittest.TestCase):
    def test_every_new_endpoint_requires_a_session(self):
        from fastapi.testclient import TestClient
        from synth.server import app as api

        @contextmanager
        def fake_connect():
            yield object()
        meeting, item = uuid4(), uuid4()
        calls = [("GET", f"/v1/meetings/{meeting}/tasks"), ("GET", f"/v1/meetings/{meeting}/decisions"), ("GET", "/v1/tasks"),
                 ("PATCH", f"/v1/tasks/{item}"), ("PATCH", f"/v1/decisions/{item}"), ("POST", f"/v1/meetings/{meeting}/regenerate")]
        with patch("synth.server.app.connect", fake_connect):
            client = TestClient(api.app)
            for method, path in calls:
                with self.subTest(path=path):
                    self.assertEqual(client.request(method, path, json={"status": "hecha"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
