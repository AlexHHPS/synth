"""Real PostgreSQL: resume, idempotency, lease/version fencing and live RLS."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
from synth.server import generation_view as view
from synth.server.db import authenticate, bind_actor, digest
from synth.worker import generations as store, queue


@unittest.skipUnless(os.environ.get("SYNTH_TEST_DATABASE_URL"), "Disposable PostgreSQL URL required")
class GenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.psycopg, self.dict_row = psycopg, dict_row
        self.url = os.environ["SYNTH_TEST_DATABASE_URL"]
        self.assertIn("@127.0.0.1:18289/", self.url)
        self.enterContext(patch.dict(os.environ, {"AUTH_MODE": "keys"}))
        self.schema = "generations_" + uuid4().hex
        self.admin = psycopg.connect(self.url, autocommit=True, row_factory=dict_row)
        self.admin.execute("""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='synth_voice_api') THEN CREATE ROLE synth_voice_api NOLOGIN; END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='synth_voice_worker') THEN CREATE ROLE synth_voice_worker NOLOGIN; END IF;
            END $$""")
        self.admin.execute("CREATE SCHEMA " + self.schema)
        self.admin.execute("SET search_path=" + self.schema)
        self.admin.execute((Path(__file__).resolve().parents[1] / "server/schema.sql").read_text())
        self.people, self.tokens = {}, {}
        for name in ("owner", "reader", "outsider", "robot"):
            self.people[name], self.tokens[name] = uuid4(), "synthetic-" + uuid4().hex
            self.admin.execute("INSERT INTO principals(id,name,kind) VALUES (%s,%s,%s)",
                               (self.people[name], name, "machine" if name == "robot" else "user"))
            self.admin.execute("INSERT INTO access_keys(id,principal_id,token_hash) VALUES (%s,%s,%s)",
                               (uuid4(), self.people[name], digest(self.tokens[name])))
        self.folder, self.mid = uuid4(), uuid4()
        self.admin.execute("INSERT INTO folders(id,owner_id,name) VALUES (%s,%s,'Synthetic shared folder')", (self.folder, self.people["owner"]))
        self.admin.execute("INSERT INTO folder_memberships VALUES (%s,%s,'reader')", (self.folder, self.people["reader"]))
        self.admin.execute("INSERT INTO meetings(id,owner_id,folder_id,title,state) VALUES (%s,%s,%s,'Synthetic meeting','queued')",
                           (self.mid, self.people["owner"], self.folder))
        self.version(1)
        queue.submit(self.admin, self.mid, "document", str(uuid4()), {"transcript_version": 1})
        with self.worker() as db:
            self.job = queue.claim(db, "worker", 300)
            self.gid = store.begin(db, self.job, "worker")

    def tearDown(self):
        self.admin.execute("DROP SCHEMA " + self.schema + " CASCADE")
        self.admin.close()

    def version(self, version):
        self.admin.execute("""INSERT INTO transcript_versions(meeting_id,version,content,content_hash,model_fingerprint,search_text)
            VALUES (%s,%s,'{}','synthetic','synthetic','')""", (self.mid, version))

    def connection(self, role="synth_voice_api"):
        db = self.psycopg.connect(self.url, autocommit=True, row_factory=self.dict_row)
        db.execute("SET ROLE " + role)
        db.execute("SET search_path=" + self.schema)
        return db

    @contextmanager
    def worker(self):
        db = self.connection("synth_voice_worker")
        db.autocommit = False
        with db:
            yield db

    @contextmanager
    def api(self, name="owner"):
        with self.connection() as db:
            actor = bind_actor(db, authenticate(db, self.tokens[name]))
            yield db, actor

    def append(self, key="delta:1", text="Acordamos revisar el plan el viernes."):
        with self.worker() as db:
            return store.append(db, self.gid, self.job, "worker", key, "delta", {"text": text})

    def test_reconnect_resumes_after_cursor_without_writes_or_duplicate_content(self):
        seq = self.append()
        self.assertEqual(seq, 2)
        self.assertEqual(self.append(), seq)
        before = self.admin.execute("SELECT attempts,state FROM jobs WHERE id=%s", (self.job["id"],)).fetchone()
        with self.api() as (db, actor):
            first = view.batch(db, actor, self.mid, self.gid, 0, 1)
            self.assertEqual((first["next_cursor"], first["more"]), (1, True))
            second = view.batch(db, actor, self.mid, self.gid, first["next_cursor"])
            self.assertEqual([e["sequence"] for e in second["events"]], [2])
            for _ in range(3):
                self.assertEqual(view.batch(db, actor, self.mid, self.gid, 2)["events"], [])
        self.assertEqual(before, self.admin.execute("SELECT attempts,state FROM jobs WHERE id=%s", (self.job["id"],)).fetchone())

    def test_reusing_a_key_with_different_content_fails_without_advancing_cursor(self):
        self.append()
        with self.assertRaisesRegex(ValueError, "generation_event_conflict"):
            self.append(text="Different synthetic content")
        self.assertEqual(self.admin.execute("SELECT last_sequence FROM document_generations WHERE id=%s", (self.gid,)).fetchone()["last_sequence"], 2)

    def test_concurrent_delivery_of_the_same_event_commits_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(lambda _: self.append(), range(4))), [2, 2, 2, 2])
        self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM document_generation_events WHERE generation_id=%s", (self.gid,)).fetchone()["n"], 2)

    def test_event_and_generation_limits_keep_the_previous_cursor(self):
        for text, error in [("x" * 61000, "generation_event_too_large"), ("x" * 300, "generation_too_large")]:
            with patch.object(store, "MAX_BYTES", 100):
                with self.assertRaisesRegex(ValueError, error):
                    self.append(text=text)
        self.assertEqual(self.admin.execute("SELECT last_sequence FROM document_generations WHERE id=%s", (self.gid,)).fetchone()["last_sequence"], 1)

    def test_cancelled_job_cannot_append_and_reader_observes_cancellation(self):
        self.admin.execute("UPDATE jobs SET state='cancelled' WHERE id=%s", (self.job["id"],))
        with self.assertRaisesRegex(ValueError, "generation_fenced"):
            self.append()
        with self.api() as (db, actor):
            self.assertEqual(view.batch(db, actor, self.mid, self.gid)["generation"]["state"], "cancelled")

    def test_new_transcript_hides_draft_and_fences_old_producer(self):
        self.version(2)
        with self.assertRaisesRegex(ValueError, "generation_fenced"):
            self.append()
        with self.api() as (db, actor):
            self.assertIsNone(view.current(db, actor, self.mid)["generation"])
            with self.assertRaises(HTTPException) as failure:
                view.batch(db, actor, self.mid, self.gid)
            self.assertEqual(failure.exception.status_code, 409)

    def test_moving_meeting_never_exposes_the_previous_folders_draft_to_its_new_readers(self):
        self.append(text='Synthetic context from the original folder')
        destination = uuid4()
        self.admin.execute("INSERT INTO folders(id,owner_id,name) VALUES (%s,%s,'Destination')", (destination,self.people['owner']))
        self.admin.execute("INSERT INTO folder_memberships VALUES (%s,%s,'reader')", (destination,self.people['outsider']))
        self.admin.execute('UPDATE meetings SET folder_id=%s WHERE id=%s', (destination,self.mid))
        with self.api('outsider') as (db,actor):
            with self.assertRaises(HTTPException) as failure:
                view.batch(db,actor,self.mid,self.gid)
            self.assertEqual(failure.exception.status_code,409)
            self.assertIsNone(view.current(db,actor,self.mid)['generation'])
        with self.assertRaisesRegex(ValueError,'generation_context_changed'):
            self.append(key='delta:2')

    def test_revoking_the_recorders_folder_access_stops_context_use_even_if_they_own_the_meeting(self):
        self.admin.execute('UPDATE folders SET owner_id=%s WHERE id=%s',(self.people['outsider'],self.folder))
        self.admin.execute("INSERT INTO folder_memberships VALUES (%s,%s,'editor')",(self.folder,self.people['owner']))
        self.append()
        self.admin.execute('DELETE FROM folder_memberships WHERE folder_id=%s AND principal_id=%s',(self.folder,self.people['owner']))
        with self.api('reader') as (db,actor):
            with self.assertRaises(HTTPException) as failure:
                view.batch(db,actor,self.mid,self.gid)
            self.assertEqual(failure.exception.status_code,409)
        with self.assertRaisesRegex(ValueError,'generation_context_changed'):
            self.append(key='delta:2')

    def test_legacy_draft_without_context_receipt_is_hidden_and_cannot_resume_production(self):
        self.admin.execute('UPDATE document_generations SET context_scope=NULL WHERE id=%s',(self.gid,))
        with self.api() as (db,actor):
            self.assertIsNone(view.current(db,actor,self.mid)['generation'])
            with self.assertRaises(HTTPException) as failure:
                view.batch(db,actor,self.mid,self.gid)
            self.assertEqual(failure.exception.status_code,409)
        with self.assertRaisesRegex(ValueError,'generation_context_changed'):
            self.append()

    def test_worker_restart_reuses_raw_response_and_fences_old_attempt(self):
        messages = [{"role": "user", "content": "Synthetic transcript"}]
        key = store.response_key(messages, 1000, "cheap-combo")
        response = {"content": '{"points": []}', "effective_model": "fixture-model"}
        with self.worker() as db:
            store.save_response(db, self.job, "worker", key, response)
        self.admin.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (self.job["id"],))
        with self.worker() as db:
            recovered = queue.claim(db, "replacement", 300)
            new_id = store.begin(db, recovered, "replacement")
            self.assertEqual(store.cached_response(db, recovered, key), response)
            self.assertIsNone(store.cached_response(db, recovered, store.response_key(messages, 1000, "local-combo")))
        self.assertNotEqual(new_id, self.gid)
        with self.assertRaisesRegex(ValueError, "generation_fenced"):
            self.append()
        with self.api() as (db, actor):
            self.assertEqual(view.current(db, actor, self.mid)["generation"]["id"], new_id)
            with self.assertRaises(HTTPException) as failure:
                view.batch(db, actor, self.mid, self.gid)
            self.assertEqual(failure.exception.status_code, 409)

    def test_current_draft_waits_during_the_gap_between_reclaim_and_new_generation(self):
        self.admin.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s",(self.job['id'],))
        with self.worker() as db:
            recovered=queue.claim(db,'replacement',300)
        with self.api() as (db,actor):
            self.assertIsNone(view.current(db,actor,self.mid)['generation'])
        with self.worker() as db:
            new_id=store.begin(db,recovered,'replacement')
        with self.api() as (db,actor):
            self.assertEqual(view.current(db,actor,self.mid)['generation']['id'],new_id)

    def test_only_readers_see_events_and_api_cannot_read_raw_model_replies(self):
        self.append()
        with self.api("reader") as (db, actor):
            self.assertEqual(len(view.batch(db, actor, self.mid, self.gid)["events"]), 2)
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                db.execute("SELECT response FROM document_model_responses")
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                db.execute("UPDATE document_generations SET state='complete'")
        for who in ("outsider", "robot"):
            with self.api(who) as (db, actor):
                self.assertEqual(db.execute("SELECT * FROM document_generation_events").fetchall(), [])
                with self.assertRaises(HTTPException) as failure:
                    view.batch(db, actor, self.mid, self.gid)
                self.assertEqual(failure.exception.status_code, 404)

    def test_publishing_corrected_version_pins_result_and_completes_once(self):
        self.append()
        self.version(2)
        with self.worker() as db:
            # The worker's fenced completion and document publication are one transaction.
            self.assertTrue(queue.finish(db, self.job["id"], "worker", 1))
            db.execute("""INSERT INTO document_versions(meeting_id,version,transcript_version,content,markdown,content_hash,model_fingerprint)
                VALUES (%s,1,2,'{}','Synthetic acta','synthetic','fixture')""", (self.mid,))
            store.close(db, self.gid, self.job, "complete", 1, 2)
            store.close(db, self.gid, self.job, "complete", 1, 2)
        with self.api() as (db, actor):
            page = view.batch(db, actor, self.mid, self.gid)
            self.assertEqual([e["kind"] for e in page["events"]], ["started", "delta", "complete"])
            self.assertEqual(page["generation"]["result_transcript_version"], 2)
            self.assertEqual(page["generation"]["state"], "complete")

    def test_publication_failure_rolls_back_document_job_and_complete_event_together(self):
        with self.assertRaisesRegex(ValueError, "synthetic_publication_failure"):
            with self.worker() as db:
                self.assertTrue(queue.finish(db, self.job["id"], "worker", 1))
                db.execute("""INSERT INTO document_versions(meeting_id,version,transcript_version,content,markdown,content_hash,model_fingerprint)
                    VALUES (%s,1,1,'{}','Synthetic acta','synthetic','fixture')""", (self.mid,))
                store.close(db, self.gid, self.job, "complete", 1, 1)
                raise ValueError("synthetic_publication_failure")
        self.assertEqual(self.admin.execute("SELECT state FROM jobs WHERE id=%s", (self.job["id"],)).fetchone()["state"], "running")
        self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM document_versions WHERE meeting_id=%s", (self.mid,)).fetchone()["n"], 0)
        self.assertEqual(self.admin.execute("SELECT last_sequence FROM document_generations WHERE id=%s", (self.gid,)).fetchone()["last_sequence"], 1)

    def test_http_routes_require_auth_and_stream_the_same_persisted_events(self):
        from fastapi.testclient import TestClient
        from synth.server import app as api
        self.append()
        with self.worker() as db:
            self.assertTrue(queue.finish(db, self.job["id"], "worker", 1, "synthetic_failure"))
            store.close(db, self.gid, self.job, "failed")
        with patch.object(api, "connect", self.connection), patch.object(view, "connect", self.connection):
            client = TestClient(api.app)
            base = f"/v1/meetings/{self.mid}"
            paths = [base + "/generation", base + f"/generations/{self.gid}/events", base + f"/generations/{self.gid}/stream"]
            for path in paths:
                self.assertEqual(client.get(path).status_code, 401)
                self.assertEqual(client.get(path, headers={"Authorization": "Bearer " + self.tokens["outsider"]}).status_code, 404)
            headers = {"Authorization": "bearer " + self.tokens["owner"]}
            page = client.get(paths[1] + "?after=1", headers=headers)
            self.assertEqual(page.status_code, 200)
            self.assertEqual([e["sequence"] for e in page.json()["events"]], [2, 3])
            streamed = client.get(paths[2] + "?after=1", headers=headers)
            self.assertEqual(streamed.status_code, 200)
            self.assertIn('"sequence": 2', streamed.text)
            self.assertNotIn('"sequence": 1', streamed.text)
            self.assertIn('"state": "failed"', streamed.text)
            self.assertEqual(streamed.headers["cache-control"], "no-store")

    async def test_new_events_arrive_live_and_closing_reader_keeps_job_running(self):
        opened = []
        def connect():
            db = self.connection()
            opened.append(db)
            return db
        with patch.object(view, "connect", connect), patch.object(view, "POLL_SECONDS", 0.01):
            iterator = view.events(self.tokens["owner"], self.people["owner"], self.mid, self.gid, 0)
            first = await anext(iterator)
            self.assertIn('"kind": "started"', first)
            self.append()
            second = await asyncio.wait_for(anext(iterator), 2)
            self.assertIn('"sequence": 2', second)
            self.assertIn("viernes", second)
            await iterator.aclose()
        self.assertTrue(opened[0].closed)
        self.assertEqual(self.admin.execute("SELECT state,attempts FROM jobs WHERE id=%s", (self.job["id"],)).fetchone(),
                         {"state": "running", "attempts": 1})

    async def test_live_membership_revocation_stops_the_next_read(self):
        with patch.object(view, "connect", self.connection), patch.object(view, "POLL_SECONDS", 0.01):
            iterator = view.events(self.tokens["reader"], self.people["reader"], self.mid, self.gid, 0)
            self.assertIn('"kind": "started"', await anext(iterator))
            self.admin.execute("DELETE FROM folder_memberships WHERE folder_id=%s AND principal_id=%s", (self.folder, self.people["reader"]))
            self.append()
            event = await asyncio.wait_for(anext(iterator), 2)
            self.assertIn("event: error", event)
            self.assertIn('"status": 404', event)
            self.assertNotIn("viernes", event)
            await iterator.aclose()


if __name__ == "__main__":
    unittest.main()
