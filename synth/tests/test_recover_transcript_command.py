import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("recover_transcript", Path(__file__).resolve().parents[1] / "scripts/recover-transcript.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RecoveryCommandTests(unittest.TestCase):
    def setUp(self):
        self.posts = []
        self.title = "Reunión sintética"
        self.meeting = "11111111-1111-4111-8111-111111111111"
        self.transcript = {"language": "es", "duration_ms": 2000, "model_fingerprint": "synthetic:test",
                           "segments": [{"id": "s0", "start_ms": 0, "end_ms": 1000,
                                         "text": "Decisión sintética", "source_id": "live_mix"}]}

    def call(self, method, path, body=None):
        if path == "/v1/me":
            return {"kind": "user"}
        if method == "POST":
            self.posts.append(body)
            return {"job_id": "job", "transcript_version": 2, "reused": len(self.posts) > 1}
        if path == "/v1/jobs/job":
            return {"state": "pending", "payload": {"sensitive": "not for output"}}
        return {"title": self.title}

    def test_default_is_read_only_and_title_mismatch_blocks_mutation(self):
        result = module.recover(self, self.meeting, self.title, self.transcript)
        self.assertFalse(result["applied"])
        self.assertFalse(self.posts)
        with self.assertRaisesRegex(ValueError, "recovery_meeting_title_changed"):
            module.recover(self, self.meeting, "Another meeting", self.transcript, True)
        self.assertFalse(self.posts)

    def test_apply_is_idempotent_and_reports_pending_without_claiming_completion(self):
        first = module.recover(self, self.meeting, self.title, self.transcript, True)
        second = module.recover(self, self.meeting, self.title, self.transcript, True)
        self.assertEqual(self.posts[0]["idempotency_key"], self.posts[1]["idempotency_key"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["job_state"], "pending")
        self.assertNotIn("payload", first)

    def test_invalid_transcript_cannot_be_uploaded(self):
        self.transcript["segments"][0]["end_ms"] = 10000
        with self.assertRaises(ValueError):
            module.recover(self, self.meeting, self.title, self.transcript, True)
        self.assertFalse(self.posts)
