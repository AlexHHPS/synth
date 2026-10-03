from pathlib import Path
import tempfile
import unittest

from synth.worker.desktop_queue import DesktopQueue, QueueError


class QueueRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.capture = self.root / "capture"
        self.capture.mkdir()
        self.file = self.capture / "sample.wav"
        self.file.write_bytes(b"RIFF-test-audio")
        self.queue = DesktopQueue(self.root / "queue.sqlite", self.capture)

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def enqueue(self, file=None):
        return self.queue.enqueue({"import": str(file or self.file)}, "Synthetic queue test", consent_confirmed=True)

    def test_duplicate_capture_and_changed_options(self):
        first = self.enqueue()
        self.assertEqual(self.enqueue()["id"], first["id"])
        with self.assertRaisesRegex(QueueError, "capture_idempotency_conflict"):
            self.queue.enqueue({"import": str(self.file)}, "Different title", consent_confirmed=True)

    def test_stable_receipt_survives_audio_retention_without_duplicate_meetings(self):
        from uuid import uuid4
        capture_id = str(uuid4())
        options = {"sources": {"import": str(self.file)}, "title": "Receipt fixture",
                   "consent_confirmed": True, "capture_id": capture_id}
        first = self.queue.enqueue(**options)
        self.file.unlink()
        replay = self.queue.enqueue(**options)
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(replay["metadata"]["sources"], first["metadata"]["sources"])
        with self.assertRaisesRegex(QueueError, "capture_idempotency_conflict"):
            self.queue.enqueue(**{**options, "title": "Changed"})
        with self.assertRaisesRegex(QueueError, "capture_idempotency_conflict"):
            self.queue.enqueue(**{**options, "notes": "Different notes"})

    def test_expired_owner_cannot_publish_and_checkpoint_survives_restart(self):
        first = self.enqueue()
        claimed = self.queue.claim(now=100, lease_seconds=10)
        self.queue.checkpoint(first["id"], claimed["lease_token"], "asr", {"sha256": "synthetic-artifact"}, now=101)
        self.queue.close()
        self.queue = DesktopQueue(self.root / "queue.sqlite", self.capture)
        recovered = self.queue.claim(now=111)
        self.assertEqual(recovered["checkpoints"]["asr"]["sha256"], "synthetic-artifact")
        self.assertNotEqual(recovered["lease_token"], claimed["lease_token"])
        with self.assertRaisesRegex(QueueError, "lease_stale"):
            self.queue.finish(first["id"], claimed["lease_token"], {"meeting_id": "wrong"}, now=112)
        result = self.queue.finish(first["id"], recovered["lease_token"], {"meeting_id": "correct"}, now=112)
        self.assertEqual(result["state"], "succeeded")

    def test_single_active_lease_across_connections(self):
        self.enqueue()
        second_file = self.capture / "second.wav"
        second_file.write_bytes(b"RIFF-second")
        second = self.enqueue(second_file)
        one = self.queue.claim(now=100)
        other = DesktopQueue(self.root / "queue.sqlite", self.capture)
        try:
            self.assertIsNone(other.claim(now=101))
            self.queue.finish(one["id"], one["lease_token"], {"ok": True}, now=102)
            self.assertEqual(other.claim(now=103)["id"], second["id"])
        finally:
            other.close()

    def test_cancelled_running_task_does_not_record_a_success_result(self):
        task = self.enqueue()
        owner = self.queue.claim(now=100)
        self.assertEqual(self.queue.cancel(task["id"])["state"], "cancel_requested")
        self.assertFalse(self.queue.heartbeat(task["id"], owner["lease_token"], now=101))
        result = self.queue.finish(task["id"], owner["lease_token"], {"document_id": "late-result"}, now=102)
        self.assertEqual(result["state"], "cancelled")
        self.assertIsNone(result["result"])

    def test_retry_retains_successful_local_stages(self):
        task = self.enqueue()
        owner = self.queue.claim(now=100)
        self.queue.checkpoint(task["id"], owner["lease_token"], "canonical", {"path": "transcript.json"}, now=101)
        self.queue.finish(task["id"], owner["lease_token"], error_code="api_unavailable", now=102)
        self.queue.retry(task["id"])
        recovered = self.queue.claim(now=103)
        self.assertEqual(recovered["checkpoints"]["canonical"]["path"], "transcript.json")

    def test_consent_and_path_boundaries(self):
        with self.assertRaisesRegex(QueueError, "capture_consent_not_confirmed"):
            self.queue.enqueue({"import": str(self.file)}, "No consent")
        outside = self.root / "private.wav"
        outside.write_bytes(b"private")
        alias = self.capture / "alias.wav"
        alias.symlink_to(outside)
        with self.assertRaisesRegex(QueueError, "capture_path_not_owned"):
            self.enqueue(alias)

    def test_checkpoint_is_immutable_and_failure_rolls_back(self):
        task = self.enqueue()
        owner = self.queue.claim(now=100)
        self.queue.checkpoint(task["id"], owner["lease_token"], "asr", {"hash": "first"}, now=101)
        with self.assertRaisesRegex(QueueError, "checkpoint_conflict"):
            self.queue.checkpoint(task["id"], owner["lease_token"], "asr", {"hash": "second"}, now=102)
        self.assertEqual(self.queue.get(task["id"])["checkpoints"]["asr"]["hash"], "first")

    def test_two_source_labels_cannot_claim_the_same_recorded_file(self):
        with self.assertRaisesRegex(QueueError, "capture_sources_share_file"):
            self.queue.enqueue({"microphone": str(self.file), "system": str(self.file)}, "Duplicate tracks", consent_confirmed=True)

    def test_failure_discards_success_result(self):
        task = self.enqueue()
        owner = self.queue.claim(now=100)
        result = self.queue.finish(task["id"], owner["lease_token"], result={"document_id": "unconfirmed"}, error_code="api_unavailable", now=101)
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(result["result"])

    def test_expired_cancel_request_stays_cancelled_after_restart(self):
        task = self.enqueue()
        owner = self.queue.claim(now=100, lease_seconds=10)
        self.queue.cancel(task["id"])
        self.queue.close()
        self.queue = DesktopQueue(self.root / "queue.sqlite", self.capture)
        self.assertIsNone(self.queue.claim(now=111))
        self.assertEqual(self.queue.get(task["id"])["state"], "cancelled")
        with self.assertRaisesRegex(QueueError, "lease_stale"):
            self.queue.finish(task["id"], owner["lease_token"], {"ok": True}, now=112)

    def test_invalid_folder_and_missing_capture_are_actionable(self):
        with self.assertRaisesRegex(QueueError, "capture_folder_invalid"):
            self.queue.enqueue({"import": str(self.file)}, "Invalid folder", folder_id="not-a-uuid", consent_confirmed=True)
        with self.assertRaisesRegex(QueueError, "capture_file_unavailable"):
            self.enqueue(self.capture / "missing.wav")


if __name__ == "__main__":
    unittest.main()
