"""Real PostgreSQL admission, replica races, lease recovery and version fencing."""
import os
from pathlib import Path
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from synth.worker import queue


@unittest.skipUnless(os.environ.get("SYNTH_TEST_DATABASE_URL"), "Disposable PostgreSQL URL required")
class AdmissionTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.psycopg, self.dict_row = psycopg, dict_row
        self.url = os.environ["SYNTH_TEST_DATABASE_URL"]
        self.assertIn("@127.0.0.1:18289/", self.url)
        self.schema = "admission_" + uuid4().hex
        self.admin = psycopg.connect(self.url, autocommit=True)
        self.admin.execute("CREATE SCHEMA " + self.schema)
        with self.connect() as db:
            db.execute((Path(__file__).resolve().parents[1] / "server/schema.sql").read_text())
            self.jobs = []
            for _ in range(4):
                owner, meeting = uuid4(), uuid4()
                db.execute("INSERT INTO principals(id,name,kind) VALUES (%s,'Synthetic user','user')", (owner,))
                db.execute("INSERT INTO meetings(id,owner_id,title,state) VALUES (%s,%s,'Synthetic meeting','queued')", (meeting, owner))
                db.execute("""INSERT INTO transcript_versions(meeting_id,version,content,content_hash,model_fingerprint,search_text)
                    VALUES (%s,1,'{}','synthetic','synthetic','')""", (meeting,))
                self.jobs.append(queue.submit(db, meeting, "document", str(uuid4()), {"transcript_version": 1})[0])

    def connect(self):
        return self.psycopg.connect(self.url, row_factory=self.dict_row,
                                   options="-c search_path=" + self.schema)

    def tearDown(self):
        self.admin.execute("DROP SCHEMA " + self.schema + " CASCADE")
        self.admin.close()

    def test_two_users_claim_but_third_waits_and_recovers_after_completion(self):
        with self.connect() as db:
            one, two = queue.claim(db, "one"), queue.claim(db, "two")
            self.assertIsNotNone(one)
            self.assertIsNotNone(two)
            self.assertNotEqual(one["meeting_id"], two["meeting_id"])
            self.assertIsNone(queue.claim(db, "three"))
            self.assertTrue(queue.finish(db, one["id"], "one", one["attempts"]))
            self.assertIsNotNone(queue.claim(db, "three"))

    def test_replicas_racing_cannot_exceed_global_capacity(self):
        barrier = threading.Barrier(4)
        def claim(number):
            with self.connect() as db:
                barrier.wait(timeout=5)
                return queue.claim(db, str(number))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(claim, range(4)))
        claimed = [r for r in results if r]
        self.assertEqual(len(claimed), 2)
        self.assertEqual(len({r["id"] for r in claimed}), 2)

    def test_expired_lease_is_reclaimed_and_old_attempt_cannot_checkpoint(self):
        with self.connect() as db:
            old = queue.claim(db, "old")
            db.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (old["id"],))
            new = queue.claim(db, "new")
            self.assertEqual(new["id"], old["id"])
            self.assertEqual(new["attempts"], old["attempts"] + 1)
            self.assertFalse(queue.checkpoint(db, old["id"], "old", old["attempts"], {}))
            self.assertFalse(queue.finish(db, old["id"], "old", old["attempts"]))

    def test_same_meeting_and_superseded_versions_are_not_admitted(self):
        with self.connect() as db:
            first = queue.claim(db, "one")
            queue.submit(db, first["meeting_id"], "document", str(uuid4()), {"transcript_version": 1})
            second = queue.claim(db, "two")
            self.assertNotEqual(second["meeting_id"], first["meeting_id"])
            db.execute("UPDATE jobs SET state='cancelled' WHERE id<>%s", (first["id"],))
            db.execute("""INSERT INTO transcript_versions(meeting_id,version,content,content_hash,model_fingerprint,search_text)
                VALUES (%s,2,'{}','synthetic2','synthetic','')""", (first["meeting_id"],))
            queue.submit(db, first["meeting_id"], "document", str(uuid4()), {"transcript_version": 2})
            self.assertIsNone(queue.claim(db, "three"))
            queue.finish(db, first["id"], "one", first["attempts"])
            current = queue.claim(db, "three")
            self.assertEqual(current["payload"]["transcript_version"], 2)

    def test_single_lane_remains_an_explicit_capacity_option(self):
        with self.connect() as db:
            self.assertIsNotNone(queue.claim(db, "one", max_running=1))
            self.assertIsNone(queue.claim(db, "two", max_running=1))


if __name__ == "__main__":
    unittest.main()
