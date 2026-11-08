"""Controlled pipe workers: ordering, lease inheritance, bounds and rejection.

These tests do not measure acoustic accuracy; a separate public native replay
checks the actual Core ML helper and encrypted outputs.
"""
import json
import os
import time
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

from synth.worker.desktop_pipeline import inference_lock
from synth.worker.speaker_session import SpeakerSession, vector


WORKER = '''
import json,sys,time
print(json.dumps({'ready':True,'protocol_version':1,'model_loads':1,'revision':'test'}),flush=True)
for line in sys.stdin:
    value=json.loads(line); path=value.get('input','')
    reply={'id':value['id'],'elapsed_seconds':0.01}
    if 'reject' in path:
        reply['error_code']='speaker_inference_failed'
    elif 'wrong' in path:
        reply.update(id='old-response',embedding=[1.]+[0.]*255,duration_ms=5000)
    elif 'huge' in path:
        sys.stdout.write('x'*(2*1024*1024+1));sys.stdout.flush();continue
    elif 'seal' in value:
        reply.update(embedding=[1.]+[0.]*255,duration_ms=5000)
    elif value['operation']=='embed':
        reply.update(embedding=[1.]+[0.]*255,duration_ms=5000)
    elif value['operation']=='project':
        reply.update(vectors=[[1.]+[0.]*127 for v in value['vectors']],embedding_space='plda_rho128_v1')
    else:
        reply.update(speech_state='detected',segments=[{'start_ms':100,'end_ms':4900,'speaker_id':'S1'}])
    print(json.dumps(reply),flush=True)
'''


class SpeakerSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.binary = self.root/'worker'
        self.binary.write_text('#!'+sys.executable+'\n'+WORKER)
        self.binary.chmod(0o700)
        self.audio = self.wav('valid')

    def wav(self, name):
        path = self.root/(name+'.wav')
        with wave.open(str(path), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0\0'*80000)
        return path

    def session(self, **kwargs):
        value = SpeakerSession(self.binary, self.root, self.root, 'test', **kwargs)
        self.addCleanup(value.close)
        return value

    def test_ordered_operations_and_rejected_crop_do_not_reload_the_worker(self):
        session = self.session()
        pid = session.process.pid
        self.assertEqual(session.embed(self.audio)['embedding'], [1.]+[0.]*255)
        self.assertEqual(session.project([[1.]+[0.]*255]), [[1.]+[0.]*127])
        self.assertEqual(session.diarize(self.audio)['segments'][0]['speaker_id'], 'S1')
        with self.assertRaisesRegex(ValueError, 'speaker_inference_failed'):
            session.embed(self.wav('reject'))
        self.assertEqual(session.embed(self.audio)['duration_ms'], 5000)
        self.assertEqual(session.process.pid, pid)
        session.close()
        self.assertIsNotNone(session.process.poll())

    def test_packaged_helper_does_not_require_a_developer_rtk_on_path(self):
        with patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}):
            session = self.session()
            self.assertEqual(session.embed(self.audio)['duration_ms'], 5000)

    def test_worker_that_stops_reading_cannot_hold_a_large_request_forever(self):
        self.binary.write_text('#!'+sys.executable+'\nimport json,time\n'
            'print(json.dumps({"ready":True,"protocol_version":1,"model_loads":1,"revision":"test"}),flush=True)\n'
            'time.sleep(60)\n')
        session = self.session(timeout=.2)
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, 'speaker_session_timeout'):
            session.project([[1.]+[0.]*255 for _ in range(128)])
        self.assertLess(time.monotonic()-started, 8)
        self.assertIsNotNone(session.process.poll())

    def test_response_from_another_request_reaps_the_worker_and_cannot_be_reused(self):
        session = self.session()
        with self.assertRaisesRegex(ValueError, 'speaker_session_response_mismatch'):
            session.embed(self.wav('wrong'))
        self.assertIsNotNone(session.process.poll())
        with self.assertRaisesRegex(ValueError, 'speaker_session_closed'):
            session.embed(self.audio)

    def test_partial_unbounded_output_is_rejected_without_waiting_for_newline(self):
        session = self.session()
        with self.assertRaisesRegex(ValueError, 'speaker_session_output_too_large'):
            session.embed(self.wav('huge'))
        self.assertIsNotNone(session.process.poll())

    def test_child_keeps_the_actual_acoustic_lease_after_parent_releases_it(self):
        state = self.root/'state'
        with inference_lock(state) as fd:
            session = self.session(lock_fd=fd)
        with self.assertRaisesRegex(ValueError, 'acoustic_worker_already_running'):
            with inference_lock(state):
                self.fail('Resident worker lost inherited lease')
        session.close()
        with inference_lock(state):
            pass

    def test_paths_and_symlinks_outside_the_capture_are_rejected_before_inference(self):
        session = self.session()
        link = self.root/'linked.wav'; link.symlink_to(self.audio)
        with self.assertRaisesRegex(ValueError, 'speaker_session_path_denied'):
            session.embed(link)
        with self.assertRaisesRegex(ValueError, 'speaker_session_path_denied'):
            session.embed(self.root.parent/'outside.wav')

    def test_sealed_request_cannot_return_a_plain_vector_to_the_caller(self):
        session = self.session()
        with self.assertRaisesRegex(ValueError, 'voice_sealed_output_invalid'):
            session.embed_sealed(self.audio, os.urandom(32), 'capture-owner-binding', self.root/'sealed.json')

    def test_invalid_biometric_vectors_are_not_normalized_into_valid_inputs(self):
        for value in ([True]+[0.]*255, [float('nan')]+[0.]*255, [0.]*256, [1.]*255):
            with self.subTest(value_type=type(value[0]).__name__), self.assertRaises(ValueError):
                vector(value, 256)

    def test_real_adapter_scope_is_lazy_shared_and_fenced_by_capture_and_lease(self):
        from synth.speakers import native
        lock = self.root/'models.json'; lock.write_text(json.dumps({'model_revision': 'test-model'}))
        worker = Mock()
        worker.embed.return_value = {'embedding': [1.]+[0.]*255}
        worker.project.return_value = [[1.]+[0.]*127]
        with patch.object(native, 'resident_session', return_value=worker) as factory, patch.object(native, 'LOCK', lock):
            with native.reuse_models(self.root, lock_fd=13):
                factory.assert_not_called()
                native.infer('embed', self.audio, self.root, lock_fd=13)
                native.project_embeddings([[1.]+[0.]*255], self.root, lock_fd=13)
                native.infer('embed', self.audio, self.root, lock_fd=13)
                self.assertEqual(factory.call_count, 1)
                with self.assertRaisesRegex(ValueError, 'speaker_session_scope_mismatch'):
                    native.infer('embed', self.audio, self.root, lock_fd=14)
                with self.assertRaisesRegex(ValueError, 'speaker_session_scope_mismatch'):
                    native.infer('embed', self.root.parent/'another-capture.wav', self.root, lock_fd=13)
                with self.assertRaisesRegex(ValueError, 'speaker_session_scope_nested'):
                    with native.reuse_models(self.root, lock_fd=13):
                        pass
            worker.close.assert_called_once()
            with native.reuse_models(self.root):
                pass
            self.assertEqual(factory.call_count, 1)
