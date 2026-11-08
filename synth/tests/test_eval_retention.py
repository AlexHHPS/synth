import json
import os
import tempfile
import unittest
from pathlib import Path

from synth.eval import retention
from synth.worker.desktop_pipeline import release_audio
from synth.worker.desktop_queue import DesktopQueue, QueueError

KEY = os.urandom(32)


def fixed_key():
    return KEY


class EvalRetentionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.capture = root / "capture"
        self.capture.mkdir()
        self.store = root / "eval"
        self.output = root / "task-output"
        self.output.mkdir()
        self.original = self.capture / "meeting.wav"
        self.original.write_bytes(b"RIFF-original-audio")
        self.processed = self.capture / "processed-t1"
        self.processed.mkdir()
        self.normalized = self.processed / "import.wav"
        self.normalized.write_bytes(b"RIFF-normalized-audio")

    def tearDown(self):
        self.directory.cleanup()

    def task(self, eval_mark):
        metadata = {"title": "Comité", "sources": {"import": {"path": str(self.original), "sha256": "x"}}}
        if eval_mark:
            metadata["eval_retention"] = True
        return {"id": "t1", "metadata": metadata}

    def release(self, eval_mark):
        normalized = {"import": {"path": str(self.normalized), "sha256": "n"}}
        return release_audio(self.task(eval_mark), self.capture, self.output, normalized,
                             eval_store=self.store, eval_key=fixed_key)

    def test_eval_meeting_keeps_only_an_encrypted_copy(self):
        result = self.release(True)
        self.assertTrue(result["eval_retained"])
        self.assertFalse(self.original.exists())
        self.assertFalse(self.normalized.exists())
        sealed = (self.store / "t1" / "import.wav.enc").read_bytes()
        self.assertNotIn(b"RIFF-normalized-audio", sealed)
        self.assertEqual(oct((self.store / "t1" / "import.wav.enc").stat().st_mode & 0o777), "0o600")
        self.assertEqual(retention.open_retained("t1", "import", self.store, fixed_key), b"RIFF-normalized-audio")
        receipt = json.loads((self.output / "audio-deletion.json").read_text())
        self.assertTrue(receipt["deleted"])
        self.assertTrue(receipt["eval_retained"])

    def test_unmarked_meeting_is_erased_without_a_copy(self):
        result = self.release(False)
        self.assertFalse(result["eval_retained"])
        self.assertFalse(self.original.exists())
        self.assertFalse(self.normalized.exists())
        self.assertFalse(self.store.exists())
        self.assertFalse(json.loads((self.output / "audio-deletion.json").read_text())["eval_retained"])

    def test_purge_erases_every_retained_meeting(self):
        self.release(True)
        forgotten = []
        self.assertEqual(retention.purge(self.store, forget_key=lambda: forgotten.append(True)), 1)
        self.assertFalse(self.store.exists())
        self.assertEqual(forgotten, [True])
        self.assertEqual(retention.purge(self.store), 0)


class EvalQueueFlagTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        (root / "capture").mkdir()
        self.file = root / "capture" / "a.wav"
        self.file.write_bytes(b"RIFF")
        self.queue = DesktopQueue(root / "q.sqlite3", root / "capture")

    def tearDown(self):
        self.queue.close()
        self.directory.cleanup()

    def test_flag_is_opt_in_and_recorded(self):
        plain = self.queue.enqueue({"import": str(self.file)}, "Normal", consent_confirmed=True)
        self.assertNotIn("eval_retention", plain["metadata"])

    def test_marked_capture_records_flag(self):
        marked = self.queue.enqueue({"import": str(self.file)}, "Eval", consent_confirmed=True, eval_retention=True)
        self.assertTrue(marked["metadata"]["eval_retention"])

    def test_non_boolean_flag_is_rejected(self):
        with self.assertRaisesRegex(QueueError, "capture_eval_flag_invalid"):
            self.queue.enqueue({"import": str(self.file)}, "Eval", consent_confirmed=True, eval_retention="yes")


if __name__ == "__main__":
    unittest.main()


class EvalRetentionFailureTests(EvalRetentionTests):
    def test_a_failing_eval_copy_never_keeps_audio(self):
        def broken_key():
            raise ValueError("voice_keychain_unavailable")
        normalized = {"import": {"path": str(self.normalized), "sha256": "n"}}
        with self.assertLogs("synth.worker", "WARNING"):
            result = release_audio(self.task(True), self.capture, self.output, normalized,
                                   eval_store=self.store, eval_key=broken_key)
        self.assertFalse(result["eval_retained"])
        self.assertFalse(self.original.exists())
        self.assertFalse(self.normalized.exists())
