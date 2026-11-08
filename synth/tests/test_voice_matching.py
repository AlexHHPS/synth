import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from synth.speakers.matching import failure_cause, identify, suggest
from synth.speakers.profiles import normalized

T = {"id": "test", "high": 0.70, "low": 0.55, "margin": 0.10, "min_seconds": 10, "single_profile_floor": 0.0}
P = [1.0] + [0.0] * 255


def towards(similarity):
    return normalized([similarity, (1 - similarity ** 2) ** 0.5] + [0.0] * 254)


class SuggestTests(unittest.TestCase):
    def test_high_score_is_a_suggestion_not_an_identity(self):
        r = suggest(towards(0.9), 30, [{"id": "p", "employee_id": "alice", "name": "Alice", "vector": P}], thresholds=T)
        self.assertEqual(r["state"], "suggested")
        self.assertEqual(r["candidate_employee_id"], "alice")
        self.assertNotIn("employee_id", r)

    def test_middle_score_asks(self):
        r = suggest(towards(0.6), 30, [{"id": "p", "employee_id": "alice", "vector": P}], thresholds=T)
        self.assertEqual(r["state"], "ask")

    def test_low_score_is_unknown(self):
        r = suggest(towards(0.3), 30, [{"id": "p", "employee_id": "alice", "vector": P}], thresholds=T)
        self.assertEqual((r["state"], r["reason"]), ("unknown", "below_threshold"))
        self.assertIsNone(r["candidate_name"])

    def test_short_cluster_abstains(self):
        r = suggest(towards(0.99), 9.9, [{"id": "p", "employee_id": "alice", "vector": P}], thresholds=T)
        self.assertEqual(r["reason"], "short_or_unclean")

    def test_two_close_employees_are_ambiguous(self):
        profiles = [{"id": "a", "employee_id": "alice", "vector": P},
                    {"id": "b", "employee_id": "bob", "vector": towards(0.98)}]
        self.assertEqual(suggest(towards(0.9), 30, profiles, thresholds=T)["reason"], "ambiguous")

    def test_reenrolling_same_person_is_not_ambiguous(self):
        profiles = [{"id": "a", "employee_id": "alice", "vector": P}, {"id": "b", "employee_id": "alice", "vector": P}]
        self.assertEqual(suggest(P, 30, profiles, thresholds=T)["state"], "suggested")

    def test_participant_prior_limits_candidates(self):
        profiles = [{"id": "a", "employee_id": "alice", "vector": P},
                    {"id": "b", "employee_id": "bob", "vector": towards(0.98)}]
        r = suggest(towards(0.9), 30, profiles, participants={"alice"}, thresholds=T)
        self.assertEqual((r["state"], r["candidate_employee_id"]), ("suggested", "alice"))
        self.assertEqual(suggest(P, 30, profiles, participants={"carol"}, thresholds=T)["reason"], "no_participant_profiles")


class FailureCauseTests(unittest.TestCase):
    def test_each_cause_has_a_safe_code(self):
        self.assertEqual(failure_cause(ValueError("speaker_binary_missing")), "speaker_binary_missing")
        self.assertEqual(failure_cause(ValueError("identity_audio_changed")), "identity_audio_changed")
        self.assertEqual(failure_cause(FileNotFoundError("/home/ana/secret.wav")), "voice_file_missing")
        self.assertEqual(failure_cause(PermissionError("x")), "voice_file_permission")
        self.assertEqual(failure_cause(OSError("disk")), "voice_io_error")
        self.assertEqual(failure_cause(KeyError("vector")), "voice_unexpected_keyerror")

    def test_personal_data_never_becomes_a_code(self):
        self.assertEqual(failure_cause(ValueError("Ana Pérez no coincide")), "voice_unexpected_valueerror")


class IdentifyTests(unittest.TestCase):
    def test_incompatible_model_abstains(self):
        with tempfile.TemporaryDirectory() as t:
            lock = Path(t) / "models.json"
            lock.write_text(json.dumps({"model_revision": "current"}))
            with patch("synth.speakers.native.LOCK", lock), \
                 patch("synth.speakers.sync.catalog", return_value=([{"id": "p", "model": "old"}], "signature", "fresh")):
                r = identify({"id": "alice"}, {}, {}, Path(t))
            self.assertEqual(r["reason"], "no_model_compatible_profiles")
            self.assertEqual(r["matches"], {})

    def test_overlapping_turns_are_excluded_from_the_print(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            lock = root / "models.json"
            lock.write_text(json.dumps({"model_revision": "current"}))
            wav = root / "capture.wav"
            wav.write_bytes(b"fixture")
            turns = [{"speaker_id": "a", "start_ms": 0, "end_ms": 15000},
                     {"speaker_id": "b", "start_ms": 5000, "end_ms": 20000}]
            profiles = [{"id": "p", "employee_id": "alice", "model": "current#crop10s", "vector": P}]
            with patch("synth.speakers.native.LOCK", lock), \
                 patch("synth.speakers.sync.catalog", return_value=(profiles, "signature", "fresh")), \
                 patch("synth.worker.journal.file_hash", return_value="hash"), \
                 patch("synth.speakers.matching.samples", return_value=[0] * 16000 * 20), \
                 patch("synth.speakers.matching.embed_chunk") as embed:
                r = identify({"id": "alice"}, {"microphone": {"path": str(wav), "sha256": "hash"}},
                             {"microphone": {"segments": turns}}, root)
            self.assertEqual({m["reason"] for m in r["matches"].values()}, {"short_or_unclean"})
            embed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
