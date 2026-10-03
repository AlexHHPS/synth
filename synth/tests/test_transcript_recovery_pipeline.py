"""Reproduce live/batch divergence through the real queue and coordinator.

Acoustic outputs and the remote API are controlled doubles. These tests verify
publication, retry and deletion behavior, not Whisper's acoustic accuracy.
"""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from synth.worker.desktop_pipeline import work_once
from synth.worker.desktop_queue import DesktopQueue


class RecordingAPI:
    auth_mode = "keys"

    def __init__(self, job_state="succeeded"):
        self.job_state = job_state
        self.uploads = []
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if path == "/v1/me":
            return {"id": "11111111-1111-4111-8111-111111111111", "kind": "user"}
        if method == "POST" and path == "/v1/meetings":
            return {"id": "meeting", "revision": 1}
        if path.endswith("/transcript"):
            self.uploads.append(body)
            return {"job_id": "document"}
        if path.endswith("/retry"):
            return {}
        if path == "/v1/jobs/document":
            return {"state": self.job_state}
        if path.endswith("/document"):
            return {"summary": "Documento simulado", "version": 1}
        raise AssertionError((method, path))


class RecoveryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.capture = self.root / "capture"
        self.source = self.capture / "meeting/sources"
        self.source.mkdir(parents=True)
        self.state = self.root / "state"
        self.database = self.state / "queue.sqlite3"
        capture_id = str(uuid4())
        (self.source / "capture.json").write_text(json.dumps({"state": "closed", "capture_id": capture_id}))
        sources = {}
        for name in ("microphone", "system"):
            path = self.source / (name + ".wav")
            path.write_bytes(b"synthetic audio placeholder")
            sources[name] = path
        self.live = ["Hoy revisamos el despliegue", "Decidimos mantener el audio en el ordenador"]
        (self.source.parent / "transcripts.json").write_text(json.dumps({"segments": [
            {"text": text, "audio_start_time": 2 + i * 3, "audio_end_time": 4 + i * 3}
            for i, text in enumerate(self.live)]}))
        queue = DesktopQueue(self.database, self.capture)
        self.task_id = queue.enqueue(sources, "Reunión sintética", consent_confirmed=True,
                                     capture_id=capture_id)["id"]
        queue.close()

    def run_pipeline(self, api, identity_callback=None):
        def asr(path, output, source_id, **kwargs):
            return {"language": "es", "duration_ms": 90000, "model_fingerprint": "synthetic:test",
                    "segments": [{"id": str(i), "start_ms": i * 1000, "end_ms": i * 1000 + 500,
                                  "text": "¡Suscríbete al canal!" if source_id == "microphone" else f"Idea {i}",
                                  "source_id": source_id} for i in range(30)]}, {}

        with patch("synth.worker.desktop_pipeline.normalize_audio", side_effect=lambda a, b, **kw: shutil.copyfile(a, b)), \
             patch("synth.worker.desktop_pipeline.transcribe", side_effect=asr) as decoder, \
             patch("synth.worker.desktop_pipeline.infer", return_value={"segments": [], "speech_state": "unknown"}), \
             patch("synth.speakers.matching.identify", return_value={"state": "unavailable", "matches": {}}, side_effect=identity_callback), \
             patch("synth.speakers.matching.profile_signature", return_value=(None, None)):
            result = work_once(self.database, self.capture, self.state, api)
            return result, decoder.call_count

    def test_live_words_are_uploaded_and_only_audio_is_deleted(self):
        api = RecordingAPI()
        result, decoded = self.run_pipeline(api)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(decoded, 2)
        uploaded = api.uploads[0]["transcript"]
        self.assertEqual([s["text"] for s in uploaded["segments"]], self.live)
        self.assertTrue(all(s["speaker_id"] is None and s["employee_id"] is None for s in uploaded["segments"]))
        self.assertFalse(list(self.capture.rglob("*.wav")))
        self.assertTrue((self.source.parent / "transcripts.json").exists())
        self.assertTrue((self.state / "tasks" / self.task_id / "document.json").exists())

    def test_missing_live_blocks_upload_and_preserves_audio_for_retry(self):
        (self.source.parent / "transcripts.json").unlink()
        api = RecordingAPI()
        result, _ = self.run_pipeline(api)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error_code"], "batch_transcription_repetitive")
        self.assertFalse(api.uploads)
        self.assertNotIn(("POST", "/v1/meetings"), api.calls)
        self.assertEqual(len(list(self.source.glob("*.wav"))), 2)
        self.assertNotIn("canonical", result["checkpoints"])

    def test_document_retry_uses_recovered_checkpoint_without_audio_or_reupload(self):
        api = RecordingAPI("failed")
        first, _ = self.run_pipeline(api)
        self.assertEqual(first["error_code"], "document_job_failed")
        self.assertFalse(list(self.capture.rglob("*.wav")))
        queue = DesktopQueue(self.database, self.capture)
        queue.retry(self.task_id)
        queue.close()
        api.job_state = "succeeded"
        second, decoded = self.run_pipeline(api)
        self.assertEqual(second["state"], "succeeded")
        self.assertEqual(decoded, 0)
        self.assertEqual(len(api.uploads), 1)
        self.assertEqual([s["text"] for s in api.uploads[0]["transcript"]["segments"]], self.live)

    def test_voice_comparison_runs_automatically_before_audio_deletion(self):
        invocations = []
        def identify(actor, sources, diarizations, raw, lock_fd):
            self.assertTrue(all(Path(d['path']).is_file() for d in sources.values()))
            self.assertEqual(set(sources), {'microphone', 'system'})
            invocations.append(actor['id'])
            return {'state': 'pilot_uncalibrated', 'active_profiles': 2, 'profile_signature': 'synthetic-catalog', 'matches': {}}
        result, _ = self.run_pipeline(RecordingAPI(), identify)
        self.assertEqual(len(invocations), 1)
        self.assertEqual(result['result']['identity']['processing_mode'], 'automatic')
        self.assertEqual(result['result']['identity']['active_profiles'], 2)
        self.assertIn('identified', result['checkpoints'])
        self.assertFalse(list(self.capture.rglob('*.wav')))
