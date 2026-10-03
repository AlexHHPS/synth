import tempfile
from pathlib import Path
import subprocess
import sys
import unittest

from synth.worker.desktop_pipeline import canonical_transcript, inference_lock, save_json, restore_json


class PipelineTests(unittest.TestCase):
    def test_one_voice_with_empty_system_audio_still_produces_transcript(self):
        mic = {"language": "es", "model_fingerprint": "whisper:test", "duration_ms": 2000,
            "segments": [{"id": "s0", "start_ms": 0, "end_ms": 1000, "text": "Mi nota personal", "source_id": "microphone"}]}
        system = {**mic, "duration_ms": 2100, "segments": [{**mic["segments"][0], "text": "Artefacto de ASR", "source_id": "system"}]}
        turns = {"segments": [{"start_ms": 0, "end_ms": 1000, "speaker_id": "speaker_0"}], "speech_state": "detected"}
        result = canonical_transcript({"microphone": mic, "system": system},
            {"microphone": turns, "system": {"speech_state": "no_usable_speech", "segments": []}})
        self.assertEqual(result["duration_ms"], 2100)
        self.assertEqual([s["text"] for s in result["segments"]], ["Mi nota personal"])
        self.assertEqual(result["segments"][0]["speaker_id"], "microphone:speaker_0")
        self.assertIsNone(result["segments"][0]["employee_id"])
        self.assertEqual(system["segments"][0]["text"], "Artefacto de ASR")

    def test_short_note_without_usable_embedding_preserves_words_and_unknown_speaker(self):
        transcript = {"language": "es", "model_fingerprint": "whisper:test", "duration_ms": 1000,
            "segments": [{"id": "s0", "start_ms": 0, "end_ms": 1000, "text": "Idea breve", "source_id": "microphone"}]}
        result = canonical_transcript({"microphone": transcript},
            {"microphone": {"speech_state": "no_usable_speech", "segments": []}})
        self.assertEqual(result["segments"][0]["text"], "Idea breve")
        self.assertIsNone(result["segments"][0]["speaker_id"])

    def test_source_provenance_and_anonymous_labels_survive_merge(self):
        source = {"language": "es", "model_fingerprint": "whisper:test", "duration_ms": 2000,
            "segments": [{"id": "s0", "start_ms": 0, "end_ms": 1000, "text": "Texto", "source_id": "import"}]}
        turns = {"segments": [{"start_ms": 0, "end_ms": 1000, "speaker_id": "speaker_0"}]}
        result = canonical_transcript({"system": source, "microphone": source},
                                      {"system": turns, "microphone": turns})
        self.assertEqual([s["id"] for s in result["segments"]], ["s000000", "s000001"])
        self.assertEqual({s["speaker_id"] for s in result["segments"]},
                         {"system:speaker_0", "microphone:speaker_0"})
        self.assertTrue(all(s["employee_id"] is None for s in result["segments"]))

    def test_corrupt_checkpoint_cannot_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = save_json(root / "content.json", {"value": 1})
            self.assertEqual(restore_json(receipt, root), {"value": 1})
            (root / "content.json").write_text('{"value":2}')
            with self.assertRaisesRegex(ValueError, "checkpoint_file_invalid"):
                restore_json(receipt, root)

    def test_checkpoint_cannot_escape_task_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "task"
            child.mkdir()
            receipt = save_json(root / "other.json", {"value": 1})
            with self.assertRaisesRegex(ValueError, "checkpoint_file_invalid"):
                restore_json(receipt, child)

    def test_os_lock_blocks_second_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            with inference_lock(directory):
                with self.assertRaisesRegex(ValueError, "acoustic_worker_already_running"):
                    with inference_lock(directory):
                        self.fail("second worker started")
            with inference_lock(directory):
                pass

    def test_inference_descendant_keeps_lock_when_parent_releases_its_fd(self):
        # Use the same rtk proxy wrapper as native inference. A surviving child
        # must prevent a supervisor from starting overlapping acoustic work.
        with tempfile.TemporaryDirectory() as directory:
            process = None
            try:
                with inference_lock(directory) as fd:
                    process = subprocess.Popen(["rtk", "proxy", sys.executable, "-c",
                        "import sys; print('ready',flush=True); sys.stdin.readline()"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, pass_fds=(fd,))
                    self.assertEqual(process.stdout.readline().strip(), "ready")
                with self.assertRaisesRegex(ValueError, "acoustic_worker_already_running"):
                    with inference_lock(directory):
                        self.fail("descendant lost lock")
                process.communicate("done\n", timeout=5)
                with inference_lock(directory):
                    pass
            finally:
                if process and process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
