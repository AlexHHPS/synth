import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from synth.speakers import matching
from synth.speakers.prints import RETENTION_SECONDS, MeetingPrints
from synth.worker.desktop_pipeline import meeting_identity, save_json

P = [1.0] + [0.0] * 255
Q = [0.0, 1.0] + [0.0] * 254
KEY = os.urandom(32)


class Clock:
    now = 1_000_000.0

    def __call__(self):
        return self.now


class MeetingPrintsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.path = Path(self.directory.name) / "prints.sqlite3"
        self.prints = MeetingPrints(self.path, KEY, clock=self.clock)

    def tearDown(self):
        self.prints.close()
        self.directory.cleanup()

    def test_prints_are_encrypted_at_rest_and_round_trip(self):
        self.prints.store("t1", "system:S1", "m", P, 42.0)
        self.assertEqual(self.prints.load("t1", "m")["system:S1"]["vector"][0], 1.0)
        raw = self.path.read_bytes()
        self.assertNotIn(json.dumps(P).encode()[:40], raw)
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_forget_erases_a_meeting(self):
        self.prints.store("t1", "system:S1", "m", P, 42.0)
        self.prints.store("t2", "system:S1", "m", Q, 42.0)
        self.assertEqual(self.prints.forget("t1"), 1)
        self.assertEqual(self.prints.load("t1", "m"), {})
        self.assertIn("system:S1", self.prints.load("t2", "m"))

    def test_prints_expire_after_thirty_days(self):
        self.prints.store("t1", "system:S1", "m", P, 42.0)
        self.clock.now += RETENTION_SECONDS - 1
        self.assertIn("system:S1", self.prints.load("t1", "m"))
        self.clock.now += 2
        self.assertEqual(self.prints.load("t1", "m"), {})

    def test_a_print_cannot_be_moved_to_another_meeting(self):
        self.prints.store("t1", "system:S1", "m", P, 42.0)
        self.prints.db.execute("UPDATE prints SET task_id='t2'")
        with self.assertRaises(Exception):
            self.prints.load("t2", "m")


class ReidentifyTests(unittest.TestCase):
    """a profile change after the audio is gone is re-evaluated from stored prints."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.output = root / "task"
        self.output.mkdir()
        self.store_path = root / "prints.sqlite3"
        prints = MeetingPrints(self.store_path, KEY)
        prints.store("task-1", "system:S1", "current#crop10s", P, 30.0)
        prints.close()
        canonical = save_json(self.output / "transcript.json", {"segments": []})
        self.checkpoints = {"canonical": canonical}
        from synth.speakers.meeting_status import transcript_hash
        save_json(self.output / "identity-status.json", {"profile_signature": "old", "transcript_hash": transcript_hash({"segments": []}),
                                                         "matches": {}})

    def tearDown(self):
        self.directory.cleanup()

    def run_identity(self, profiles):
        lock = Path(self.directory.name) / "models.json"
        lock.write_text(json.dumps({"model_revision": "current"}))
        with patch("synth.speakers.prints.open_prints", lambda: MeetingPrints(self.store_path, KEY)), \
             patch("synth.speakers.native.LOCK", lock), \
             patch("synth.speakers.sync.catalog", return_value=(profiles, "new", "fresh")):
            return meeting_identity({"id": "alice"}, {"id": "task-1"}, self.checkpoints, self.output, None)

    def test_profile_change_with_deleted_audio_is_reidentified_from_prints(self):
        profiles = [{"id": "p", "employee_id": "alice", "name": "Alice", "model": "current#crop10s", "vector": P}]
        identity = self.run_identity(profiles)
        self.assertNotEqual(identity.get("reason"), "profile_changed_audio_deleted")
        self.assertEqual(identity["source"], "stored_prints")
        self.assertEqual(identity["matches"]["system:S1"]["state"], "suggested")

    def test_without_prints_the_reason_is_explicit(self):
        MeetingPrints(self.store_path, KEY).forget("task-1")
        identity = self.run_identity([{"id": "p", "employee_id": "alice", "model": "current#crop10s", "vector": P}])
        self.assertEqual(identity["reason"], "voiceprints_unavailable")


class FailureCauseRecordedTests(unittest.TestCase):
    """voice_comparison_failed always carries a non-personal cause code."""

    def test_each_failure_records_its_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            checkpoints = {"canonical": save_json(output / "transcript.json", {"segments": []}),
                           "diarization": save_json(output / "diarization.json", {}), "normalized": {}}
            for error, cause in ((ValueError("speaker_binary_missing"), "speaker_binary_missing"),
                                 (ValueError("identity_audio_changed"), "identity_audio_changed"),
                                 (FileNotFoundError("/home/ana/x.wav"), "voice_file_missing"),
                                 (OSError("disk"), "voice_io_error")):
                with patch("synth.speakers.matching.identify", side_effect=error), \
                     self.assertLogs("synth.worker", "WARNING") as logs:
                    identity = meeting_identity({"id": "a"}, {"id": "t"}, checkpoints, output, output)
                self.assertEqual((identity["reason"], identity["cause"]), ("voice_comparison_failed", cause))
                self.assertIn("cause=" + cause, logs.output[0])
                self.assertNotIn("/home/ana", logs.output[0])


class NothingLeavesTheHostTests(unittest.TestCase):
    def test_identity_result_carries_no_vectors(self):
        prints = {"system:S1": {"vector": P, "clean_seconds": 30.0}}
        result = matching.classify_prints(prints, [{"id": "p", "employee_id": "a", "name": "A", "vector": P}])
        self.assertNotIn("vector", json.dumps(result))
        self.assertNotIn("1.0, 0.0, 0.0", json.dumps(result))



class ResolveSpeakerTests(unittest.TestCase):
    def test_confirming_a_speaker_erases_only_that_voiceprint(self):
        from synth.speakers.voice_actions import resolve_speaker
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prints.sqlite3"
            store = MeetingPrints(path, KEY)
            store.store("task-1", "system:S1", "m", P, 30.0)
            store.store("task-1", "system:S2", "m", Q, 30.0)
            store.close()
            calls = []

            class API:
                def call(self, method, route, body):
                    calls.append((method, route, body["action"]))
                    return {"transcript_version": 2}
            with patch("synth.speakers.prints.open_prints", lambda: MeetingPrints(path, KEY)):
                resolve_speaker(API(), "8884ad49-1f74-4668-80d2-b575a248eac8", "task-1",
                                {"action": "confirm", "speaker_id": "system:S1", "name": "Ana", "expected_version": 1})
            remaining = MeetingPrints(path, KEY).load("task-1", "m")
            self.assertEqual(set(remaining), {"system:S2"})
            self.assertEqual(calls, [("POST", "/v1/meetings/8884ad49-1f74-4668-80d2-b575a248eac8/speakers", "confirm")])

    def test_invalid_action_is_refused_before_any_call(self):
        from synth.speakers.voice_actions import resolve_speaker
        with self.assertRaises(ValueError):
            resolve_speaker(None, "x", "t", {"action": "delete_everything", "speaker_id": "s"})


class MoreFailureCausesTests(unittest.TestCase):
    def test_keychain_timeout_key_mismatch_and_busy_store_have_codes(self):
        import sqlite3
        import subprocess
        from cryptography.exceptions import InvalidTag
        from synth.speakers.matching import failure_cause
        self.assertEqual(failure_cause(subprocess.TimeoutExpired("security", 15)), "voice_keychain_timeout")
        self.assertEqual(failure_cause(InvalidTag()), "voice_key_mismatch")
        self.assertEqual(failure_cause(sqlite3.OperationalError("database is locked")), "voice_store_busy")

    def test_any_identity_error_is_recorded_not_raised(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            checkpoints = {"canonical": save_json(output / "transcript.json", {"segments": []}),
                           "diarization": save_json(output / "diarization.json", {}), "normalized": {}}
            with patch("synth.speakers.matching.identify", side_effect=RuntimeError("boom")), \
                 self.assertLogs("synth.worker", "WARNING"):
                identity = meeting_identity({"id": "a"}, {"id": "t"}, checkpoints, output, output)
            self.assertEqual(identity["cause"], "voice_unexpected_runtimeerror")

    def test_participants_from_the_capture_restrict_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            checkpoints = {"canonical": save_json(output / "transcript.json", {"segments": []}),
                           "diarization": save_json(output / "diarization.json", {}), "normalized": {}}
            seen = {}

            def identify(*args, **options):
                seen.update(options)
                return {"state": "suggestions", "matches": {}}
            with patch("synth.speakers.matching.identify", identify):
                meeting_identity({"id": "a"}, {"id": "t", "metadata": {"participants": ["p1", "p2"]}}, checkpoints, output, output)
            self.assertEqual(seen["participants"], {"p1", "p2"})


class RetriedDecisionTests(unittest.TestCase):
    def test_lost_response_retry_still_erases_the_print(self):
        from synth.speakers.voice_actions import resolve_speaker
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prints.sqlite3"
            store = MeetingPrints(path, KEY)
            store.store("task-1", "system:S1", "m", P, 30.0)
            store.close()

            class API:
                def call(self, method, route, body=None):
                    if method == "POST":
                        raise ValueError("api_http_409")
                    return {"content": {"segments": [{"speaker_id": "system:S1"}],
                                        "speakers": {"system:S1": {"status": "confirmed", "name": "Ana"}}}}
            with patch("synth.speakers.prints.open_prints", lambda: MeetingPrints(path, KEY)):
                with self.assertRaises(ValueError):
                    resolve_speaker(API(), "8884ad49-1f74-4668-80d2-b575a248eac8", "task-1",
                                    {"action": "confirm", "speaker_id": "system:S1", "name": "Ana", "expected_version": 1})
            self.assertEqual(MeetingPrints(path, KEY).load("task-1", "m"), {})


if __name__ == "__main__":
    unittest.main()
