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
        if path.startswith("/v1/glossary"):
            return {"items": []}
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
        if path == '/v1/meetings/meeting/jobs':
            return {'items': [{'id': 'document', 'state': self.job_state, 'is_current': True,
                               'transcript_version': 1, 'attempts': 1}]}
        if path.endswith("/document"):
            return {"summary": "Documento simulado", "version": 1}
        raise AssertionError((method, path))


class PipelineFixture(unittest.TestCase):
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

    # A generic catastrophic repetition (not a known silence hallucination) still forces the live fallback.
    repeated = "Lo dejamos así entonces"

    def run_pipeline(self, api, identity_callback=None, asr_callback=None, acoustic_callback=None, inference_callback=None):
        def asr(path, output, source_id, **kwargs):
            return {"language": "es", "duration_ms": 90000, "model_fingerprint": "synthetic:test",
                    "segments": [{"id": str(i), "start_ms": i * 1000, "end_ms": i * 1000 + 500,
                                  "text": self.repeated if source_id == "microphone" else f"Idea {i}",
                                  "source_id": source_id} for i in range(30)]}, {}

        with patch("synth.worker.desktop_pipeline.normalize_audio", side_effect=lambda a, b, **kw: shutil.copyfile(a, b)), \
             patch("synth.worker.desktop_pipeline.prepare_acoustic_sources", side_effect=acoustic_callback or (lambda normalized, raw, **kw: {'sources': normalized, 'echo': {'skipped': 'synthetic'}})), \
             patch("synth.worker.desktop_pipeline.transcribe", side_effect=asr_callback or asr) as decoder, \
             patch("synth.worker.desktop_pipeline.infer", return_value={"segments": [], "speech_state": "unknown"}, side_effect=inference_callback), \
             patch("synth.speakers.matching.identify", return_value={"state": "unavailable", "matches": {}}, side_effect=identity_callback), \
             patch("synth.speakers.matching.profile_signature", return_value=(None, None)):
            result = work_once(self.database, self.capture, self.state, api)
            return result, decoder.call_count

class RecoveryPipelineTests(PipelineFixture):
    def test_owner_bound_capture_cannot_replace_failed_batch_with_short_live_prefix(self):
        capture_id = json.loads((self.source/'capture.json').read_text())['capture_id']
        (self.source.parent/'capture-owner.json').write_text(json.dumps({
            'version': 1, 'capture_id': capture_id, 'identity': '11111111-1111-4111-8111-111111111111'}))
        api = RecordingAPI()
        result, _ = self.run_pipeline(api)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['error_code'], 'batch_transcription_repetitive')
        self.assertFalse(api.uploads)
        self.assertTrue(list(self.capture.rglob('*.wav')))
        self.assertNotIn('canonical', result['checkpoints'])

    def test_unexpected_failure_is_recorded_instead_of_reclaimed_forever(self):
        api = RecordingAPI()
        with patch('synth.worker.desktop_pipeline.canonical_transcript', side_effect=RuntimeError('private transcript detail')), \
             self.assertLogs('synth.worker', level='ERROR') as logs:
            first, _ = self.run_pipeline(api)
        self.assertEqual(first['state'], 'failed')
        self.assertEqual(first['error_code'], 'host_pipeline_failed')
        self.assertIsNone(first['lease_token'])
        self.assertNotIn('private transcript detail', '\n'.join(logs.output))
        self.assertTrue(list(self.capture.rglob('*.wav')))
        second, decoded = self.run_pipeline(api)
        self.assertIsNone(second)
        self.assertEqual(decoded, 0)

    def test_voice_confirmation_retry_after_upload_reuses_text_and_deleted_audio(self):
        from synth.server.desktop_host import public_task
        from synth.server.task_status import document_action, project_task

        class VoiceAPI(RecordingAPI):
            def __init__(self):
                super().__init__()
                self.batches = []

            def call(self, method, path, body=None):
                if path.endswith('/transcript'):
                    super().call(method, path, body)
                    return {'job_id': 'document', 'transcript_version': 1}
                if path.endswith('/speakers/batch'):
                    self.calls.append((method, path))
                    self.batches.append(body)
                    if len(self.batches) == 1:
                        raise ValueError('api_unavailable')
                    return {'job_id': 'named-document', 'transcript_version': 2}
                if path == '/v1/meetings/meeting/jobs' and len(self.batches) >= 2:
                    return {'items': [{'id': 'named-document', 'state': 'queued', 'is_current': True,
                                       'transcript_version': 2, 'attempts': 0}]}
                return super().call(method, path, body)

        api = VoiceAPI()
        action = {'action': 'confirm', 'speaker_id': 'live-confirmed:fixture',
                  'employee_id': '22222222-2222-4222-8222-222222222222', 'name': 'Persona sintética'}
        with patch('synth.speakers.finalize_capture.export_confirmations',
                   side_effect=lambda content, *args: (content, [action])):
            first, decoded = self.run_pipeline(api)
            self.assertEqual(first['state'], 'failed')
            self.assertEqual(decoded, 2)
            self.assertIn('uploaded', first['checkpoints'])
            self.assertIn('voice_plan', first['checkpoints'])
            self.assertNotIn('voice_applied', first['checkpoints'])
            self.assertFalse(list(self.capture.rglob('*.wav')))
            view = project_task(public_task(first), api)
            self.assertEqual(view['state'], 'failed')
            self.assertEqual(view['stage'], 'confirming_voices')
            self.assertTrue(view['voice_confirmation_pending'])
            self.assertTrue(view['can_retry'])
            self.assertFalse(document_action(api, first, 'retry'))
            queue = DesktopQueue(self.database, self.capture)
            try:
                queue.retry(first['id'])
            finally:
                queue.close()
            second, decoded = self.run_pipeline(api)

        self.assertEqual(second['state'], 'succeeded')
        self.assertEqual(decoded, 0)
        self.assertEqual(len(api.uploads), 1)
        self.assertEqual(api.calls.count(('POST', '/v1/meetings')), 1)
        self.assertFalse(any(path.endswith('/retry') for method, path in api.calls))
        self.assertEqual(api.batches[0], api.batches[1])
        self.assertEqual(second['result']['job_id'], 'named-document')
        self.assertIn('voice_applied', second['checkpoints'])
        view = project_task(public_task(second), api)
        self.assertEqual(view['state'], 'queued')
        self.assertEqual(view['transcript_version'], 2)
        self.assertFalse(view['voice_confirmation_pending'])

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
        self.assertEqual(result['result']['job_id'], 'document')
        self.assertNotIn(('GET', '/v1/jobs/document'), api.calls)

    def test_second_source_failure_retries_only_unfinished_source(self):
        calls = []
        def decode(path, output, source_id, **kwargs):
            calls.append(source_id)
            if source_id == 'system' and calls.count('system') == 1:
                raise ValueError('qwen_alignment_invalid_time')
            return {'language': 'es', 'duration_ms': 90000, 'model_fingerprint': 'qwen:test',
                    'segments': [{'id': 's0', 'start_ms': 1000, 'end_ms': 2000,
                                  'text': 'Hola equipo' if source_id == 'microphone' else 'Revisamos la propuesta',
                                  'source_id': source_id}]}, {}
        api = RecordingAPI()
        first, _ = self.run_pipeline(api, asr_callback=decode)
        self.assertEqual(first['error_code'], 'qwen_alignment_invalid_time')
        self.assertIn('asr_microphone', first['checkpoints'])
        self.assertNotIn('asr', first['checkpoints'])
        self.assertFalse(api.uploads)
        self.assertEqual(len(list(self.source.glob('*.wav'))), 2)
        queue = DesktopQueue(self.database, self.capture)
        queue.retry(self.task_id); queue.close()
        second, decoded = self.run_pipeline(api, asr_callback=decode)
        self.assertEqual(second['state'], 'succeeded')
        self.assertEqual(decoded, 1)
        self.assertEqual(calls, ['microphone', 'system', 'system'])
        self.assertEqual([s['text'] for s in api.uploads[0]['transcript']['segments']],
                         ['Hola equipo', 'Revisamos la propuesta'])

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
        report = json.loads((self.state/'tasks'/self.task_id/'transcript-quality.json').read_text())
        self.assertEqual(report['selected'], 'blocked')

    def test_duplicate_sources_without_live_block_publication_and_audio_deletion(self):
        (self.source.parent / 'transcripts.json').unlink()
        def duplicate_asr(path, output, source_id, **kwargs):
            return {'language': 'es', 'duration_ms': 90000, 'model_fingerprint': 'synthetic:test',
                    'segments': [{'id': str(i), 'start_ms': i*5000, 'end_ms': i*5000+4500,
                                  'text': f'Revisamos el documento completo de la propuesta con todo el equipo {i}',
                                  'source_id': source_id} for i in range(6)]}, {}
        api = RecordingAPI()
        result, _ = self.run_pipeline(api, asr_callback=duplicate_asr)
        self.assertEqual(result['error_code'], 'batch_cross_source_duplication')
        self.assertFalse(api.uploads)
        self.assertNotIn(('POST', '/v1/meetings'), api.calls)
        self.assertEqual(len(list(self.source.glob('*.wav'))), 2)
        self.assertNotIn('canonical', result['checkpoints'])
        report = json.loads((self.state/'tasks'/self.task_id/'transcript-quality.json').read_text())
        self.assertEqual(report['selected'], 'blocked')
        self.assertTrue(report['cross_source']['suspicious'])

    def test_cloud_failure_and_retry_do_not_requeue_completed_acoustics(self):
        from synth.server.task_status import document_action, project_task
        from synth.server.desktop_host import public_task
        api = RecordingAPI("failed")
        first, _ = self.run_pipeline(api)
        self.assertEqual(first['state'], 'succeeded')
        view = project_task(public_task(first), api)
        self.assertEqual(view['state'], 'failed')
        self.assertTrue(view['can_retry'])
        self.assertFalse(list(self.capture.rglob("*.wav")))
        self.assertTrue(document_action(api, first, 'retry'))
        self.assertIn(('POST', '/v1/jobs/document/retry'), api.calls)
        api.job_state = "succeeded"
        second, decoded = self.run_pipeline(api)
        self.assertIsNone(second)
        self.assertEqual(decoded, 0)
        self.assertEqual(project_task(public_task(first), api)['state'], 'succeeded')
        self.assertEqual(len(api.uploads), 1)
        self.assertEqual([s["text"] for s in api.uploads[0]["transcript"]["segments"]], self.live)

    def test_pending_cloud_document_does_not_hold_acoustic_lane(self):
        api = RecordingAPI('running')
        first, _ = self.run_pipeline(api)
        self.assertEqual(first['state'], 'succeeded')
        self.assertNotIn(('GET', '/v1/jobs/document'), api.calls)
        # A second capture can be processed while the first cloud job is pending.
        source = self.capture/'second/sources'; source.mkdir(parents=True)
        identifier = str(uuid4())
        (source/'capture.json').write_text(json.dumps({'state': 'closed', 'capture_id': identifier}))
        (source.parent/'transcripts.json').write_text(json.dumps({'segments': [
            {'text': 'Segunda reunión', 'audio_start_time': 0, 'audio_end_time': 1}]}))
        paths = {}
        for name in ['microphone', 'system']:
            paths[name] = source/(name+'.wav'); paths[name].write_bytes(b'synthetic placeholder')
        queue = DesktopQueue(self.database, self.capture)
        queue.enqueue(paths, 'Segunda reunión', consent_confirmed=True, capture_id=identifier)
        queue.close()
        second, decoded = self.run_pipeline(api)
        self.assertEqual(second['state'], 'succeeded')
        self.assertEqual(decoded, 2)
        self.assertEqual(len(api.uploads), 2)

    def test_ambiguous_upload_failure_retries_exact_receipt_without_audio(self):
        class UncertainAPI(RecordingAPI):
            fail_once = True
            def call(self, method, path, body=None):
                result = super().call(method, path, body)
                if path.endswith('/transcript') and self.fail_once:
                    self.fail_once = False
                    raise ValueError('api_unavailable')
                return result
        api = UncertainAPI()
        first, _ = self.run_pipeline(api)
        self.assertEqual(first['error_code'], 'api_unavailable')
        self.assertFalse(list(self.capture.rglob('*.wav')))
        queue = DesktopQueue(self.database, self.capture);queue.retry(self.task_id);queue.close()
        second, decoded = self.run_pipeline(api)
        self.assertEqual(second['state'], 'succeeded')
        self.assertEqual(decoded, 0)
        self.assertEqual(api.uploads[0], api.uploads[1])

    def test_voice_comparison_runs_automatically_before_audio_deletion(self):
        invocations = []
        def identify(actor, sources, diarizations, raw, lock_fd, **options):
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

    def test_asr_diarization_and_recognition_share_cancelled_microphone(self):
        seen = set()
        def prepare(normalized, raw, **kwargs):
            from synth.worker.journal import file_hash
            target = raw/'microphone.aec.wav'; target.write_bytes(b'cancelled synthetic audio')
            return {'sources': {**normalized, 'microphone': {'path': str(target), 'sha256': file_hash(target)}},
                    'echo': {'engine': 'synthetic_aec'}}
        def identify(actor, sources, diarizations, raw, lock_fd, **options):
            self.assertTrue(sources['microphone']['path'].endswith('microphone.aec.wav'))
            self.assertTrue(Path(sources['microphone']['path']).exists())
            seen.add('identity')
            return {'state': 'unavailable', 'matches': {}}
        def asr(path, output, source_id, **kwargs):
            if source_id == 'microphone':
                self.assertTrue(str(path).endswith('microphone.aec.wav'));seen.add('asr')
            return {'language': 'es', 'duration_ms': 90000, 'model_fingerprint': 'synthetic:test',
                    'segments': [{'id': '0', 'text': 'Texto breve', 'start_ms': 0, 'end_ms': 2000,
                                  'source_id': source_id}]}, {}
        def infer(operation, path, raw, **kwargs):
            if Path(path).name != 'system.wav':
                self.assertTrue(str(path).endswith('microphone.aec.wav'));seen.add('diarization')
            return {'segments': [], 'speech_state': 'unknown'}
        result, _ = self.run_pipeline(RecordingAPI(), identify, asr_callback=asr,
                                      acoustic_callback=prepare, inference_callback=infer)
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(seen, {'asr', 'diarization', 'identity'})
        self.assertIn('acoustic', result['checkpoints'])
        prepared = json.loads((self.state/'tasks'/self.task_id/'acoustic.json').read_text())
        self.assertEqual(prepared['echo']['engine'], 'synthetic_aec')
        self.assertFalse(list(self.capture.rglob('*.wav')))

    def test_legacy_asr_checkpoint_is_not_combined_with_new_audio_processing(self):
        first, _ = self.run_pipeline(RecordingAPI(), inference_callback=lambda *a, **kw: (_ for _ in ()).throw(ValueError('speaker_inference_failed')))
        self.assertEqual(first['state'], 'failed')
        self.assertIn('asr', first['checkpoints'])
        queue = DesktopQueue(self.database, self.capture)
        # Reproduce a checkpoint made by the host before AEC was introduced.
        queue.db.execute("UPDATE desktop_tasks SET checkpoints=json_remove(checkpoints,'$.acoustic') WHERE id=?", (self.task_id,))
        queue.db.commit()
        queue.retry(self.task_id); queue.close()
        def unexpected(*args, **kwargs):
            self.fail('Legacy decoded audio was processed again')
        result, decoded = self.run_pipeline(RecordingAPI(), acoustic_callback=unexpected)
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(decoded, 0)

class KnownHallucinationPipelineTests(PipelineFixture):
    """A microphone that only produced «¡Suscríbete al canal!» keeps the batch transcript (with speakers)."""
    repeated = "¡Suscríbete al canal!"

    def test_known_hallucination_is_dropped_and_batch_kept(self):
        api = RecordingAPI()
        result, _ = self.run_pipeline(api)
        self.assertEqual(result["state"], "succeeded")
        texts = [s["text"] for s in api.uploads[0]["transcript"]["segments"]]
        self.assertEqual(texts, [f"Idea {i}" for i in range(30)])
        self.assertNotIn("¡Suscríbete al canal!", " ".join(texts))

    def test_known_hallucination_does_not_need_the_live_capture(self):
        (self.source.parent / "transcripts.json").unlink()
        result, _ = self.run_pipeline(RecordingAPI())
        self.assertEqual(result["state"], "succeeded")


class SuggestionNeverNamesTests(PipelineFixture):
    """a high-confidence suggestion never puts a name in the uploaded transcript."""
    repeated = "Lo dejamos así entonces"

    def test_suggested_candidate_name_is_not_uploaded(self):
        def identify(actor, sources, diarizations, raw, lock_fd, **options):
            return {"state": "suggestions", "profile_signature": "s", "matches": {
                "system:S1": {"state": "suggested", "candidate_name": "María Candidata",
                              "candidate_employee_id": "22222222-2222-4222-8222-222222222222", "similarity": 0.91}}}
        api = RecordingAPI()
        result, _ = self.run_pipeline(api, identify)
        self.assertEqual(result["state"], "succeeded")
        uploaded = json.dumps(api.uploads[0]["transcript"], ensure_ascii=False)
        self.assertNotIn("María Candidata", uploaded)
        self.assertNotIn("22222222-2222-4222-8222-222222222222", uploaded)
        self.assertNotIn("speakers", api.uploads[0]["transcript"])
        self.assertTrue(all(s["employee_id"] is None for s in api.uploads[0]["transcript"]["segments"]))
