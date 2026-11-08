"""Prepared audio/receipt roundtrip, including interrupted inference and tampering."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import wave

import numpy as np

from synth.worker.consolidated_capture import PreparedTracks, file_hash, load, publish
from synth.worker.consolidation import ConsolidationJournal, RATE
from synth.worker.contextual_audio import ContextualProcessor
from synth.worker.incremental_audio import SourceReader


class ConsolidatedCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root/'sources').mkdir()
        self.capture = str(uuid4())
        self.sources = {}
        for source in ('microphone', 'system'):
            path = self.root/'sources'/f'{source}.wav'
            with wave.open(str(path), 'wb') as audio:
                audio.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
                audio.writeframes(np.full(24*RATE, 8192, dtype='<i2').tobytes())
            self.sources[source] = {'path': str(path), 'sha256': file_hash(path), 'frames': 24*RATE}
        self.progress = {'version': 1, 'capture_id': self.capture, 'state': 'closed',
                         'clock': 'recording_elapsed', 'sample_rate': RATE, 'sample_format': 'pcm16',
                         'sources': {s: 24*RATE for s in self.sources}}
        (self.root/'sources/progress.json').write_text(json.dumps(self.progress))
        (self.root/'sources/capture.json').write_text(json.dumps({**self.progress, 'sources': self.sources}))
        self.policy = {'synthetic_model': 'test-only'}
        self.journal = ConsolidationJournal(self.root/'journal.sqlite3', self.capture, self.policy)
        self.addCleanup(self.journal.close)
        self.calls = []

    def process(self, decode=None):
        prepared = PreparedTracks(self.root, self.sources)
        self.addCleanup(prepared.close)
        def recognize(source, audio, start, end):
            self.calls.append((source, start))
            return {'text': 'No.'}
        def align(source, audio, text, start):
            return [{'text': text, 'start_ms': 11920 if start == 0 else 11880,
                     'end_ms': 12080 if start == 0 else 12040}]
        processor = ContextualProcessor(SourceReader(self.root), self.journal, decode or recognize, align,
            lambda sources: (sources, {s: {'has_speech': True} for s in sources}), prepared.append)
        for _ in range(30):
            if processor.poll()['state'] == 'drained':
                return prepared
        self.fail('Capture did not drain')

    def export(self, prepared):
        return publish(self.root, self.progress, self.journal, prepared, self.policy)

    def test_roundtrip_preserves_raw_hypotheses_and_one_owned_negation(self):
        receipt = self.export(self.process())
        self.assertEqual(load(self.root, self.capture, self.sources), receipt)
        self.assertEqual(len(receipt['asr_evidence']), 4)
        self.assertTrue(all(row['raw_text'] == 'No.' for row in receipt['asr_evidence']))
        for source in self.sources:
            transcript = receipt['transcripts'][source]
            self.assertEqual([s['text'] for s in transcript['segments']], ['No.'])
            self.assertEqual(len(transcript['words']), 1)
            self.assertEqual(transcript['segments'][0]['evidence_state'], 'aligned')
            self.assertTrue(transcript['segments'][0]['asr_block_id'].startswith(self.capture+':'+source+':'))
            descriptor = receipt['prepared_sources'][source]
            self.assertEqual(descriptor['frames'], 24*16000)
            self.assertEqual((self.root/descriptor['path']).stat().st_mode & 0o777, 0o600)

    def test_replay_rebuilds_prepared_audio_without_new_asr_and_republishes(self):
        first = self.export(self.process())
        def forbidden(*args):
            self.fail('Completed model work was repeated')
        second = self.export(self.process(forbidden))
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(first['transcripts'], second['transcripts'])
        self.assertEqual(first['asr_evidence'], second['asr_evidence'])
        for source in self.sources:
            self.assertNotEqual(first['prepared_sources'][source]['path'], second['prepared_sources'][source]['path'])
            self.assertEqual(first['prepared_sources'][source]['sha256'], second['prepared_sources'][source]['sha256'])
        self.assertEqual(load(self.root, self.capture, self.sources), second)

    def test_no_receipt_for_a_different_capture_or_mutated_raw_source(self):
        self.export(self.process())
        with self.assertRaisesRegex(ValueError, 'consolidation_receipt_invalid'):
            load(self.root, str(uuid4()), self.sources)
        path = Path(self.sources['system']['path'])
        with path.open('r+b') as stream:
            stream.seek(100); stream.write(b'\0\0')
        with self.assertRaisesRegex(ValueError, 'consolidation_source_changed'):
            load(self.root, self.capture, self.sources)

    def test_prepared_audio_hash_and_directory_cannot_be_substituted(self):
        receipt = self.export(self.process())
        path = self.root/receipt['prepared_sources']['system']['path']
        actual = path.with_suffix('.saved.wav')
        path.rename(actual); path.symlink_to(actual)
        with self.assertRaisesRegex(ValueError, 'consolidation_prepared_changed'):
            load(self.root, self.capture, self.sources)
        path.unlink(); actual.rename(path)
        with path.open('r+b') as stream:
            stream.seek(100); stream.write(b'\0\0')
        with self.assertRaisesRegex(ValueError, 'consolidation_prepared_changed'):
            load(self.root, self.capture, self.sources)

    def test_native_close_receipt_is_required_before_publication(self):
        prepared = self.process()
        (self.root/'sources/capture.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.export(prepared)
        self.assertFalse((self.root/'consolidated-capture.json').exists())

    def test_final_worker_reuses_live_asr_and_prepared_audio_without_decoding_again(self):
        from synth.tests.test_transcript_recovery_pipeline import RecordingAPI
        from synth.worker.desktop_pipeline import work_once
        from synth.worker.desktop_queue import DesktopQueue
        receipt = self.export(self.process())
        state = self.root/'pipeline'
        queue = DesktopQueue(state/'queue.sqlite3', self.root)
        queue.enqueue({s: d['path'] for s, d in self.sources.items()}, 'Synthetic live reuse',
                      consent_confirmed=True, capture_id=self.capture)
        queue.close()
        api = RecordingAPI()
        def forbidden(*args, **kwargs):
            self.fail('Final worker repeated ASR or acoustic preparation')
        def identify(actor, sources, diarizations, raw, lock_fd, **kwargs):
            self.assertEqual({s: d['sha256'] for s, d in sources.items()},
                             {s: d['sha256'] for s, d in receipt['prepared_sources'].items()})
            self.assertTrue(all(Path(d['path']).is_file() for d in sources.values()))
            return {'state': 'unavailable', 'matches': {}}
        with patch('synth.worker.desktop_pipeline.normalize_audio', side_effect=forbidden), \
             patch('synth.worker.desktop_pipeline.prepare_acoustic_sources', side_effect=forbidden), \
             patch('synth.worker.desktop_pipeline.transcribe', side_effect=forbidden), \
             patch('synth.worker.desktop_pipeline.infer', return_value={'segments': [], 'speech_state': 'unknown'}), \
             patch('synth.speakers.matching.identify', side_effect=identify):
            result = work_once(state/'queue.sqlite3', self.root, state, api)
        self.assertEqual(result['state'], 'succeeded', result.get('error_code'))
        self.assertIn('consolidated', result['checkpoints'])
        uploaded = api.uploads[0]['transcript']
        self.assertEqual([s['text'] for s in uploaded['segments']], ['No.', 'No.'])
        self.assertEqual(uploaded['asr_evidence'], receipt['asr_evidence'])
        self.assertEqual({s['source_id'] for s in uploaded['segments']}, {'microphone', 'system'})
        self.assertTrue(all(s['evidence_state'] == 'aligned' for s in uploaded['segments']))
        self.assertFalse(list(self.root.rglob('*.wav')))
        self.assertEqual(len(api.uploads), 1)

    def test_adoption_interruption_then_uncertain_upload_recovers_without_new_asr(self):
        from synth.tests.test_transcript_recovery_pipeline import RecordingAPI
        from synth.worker.desktop_pipeline import work_once
        from synth.worker.desktop_queue import DesktopQueue
        self.export(self.process())
        state = self.root/'pipeline'
        queue = DesktopQueue(state/'queue.sqlite3', self.root)
        task = queue.enqueue({s: d['path'] for s, d in self.sources.items()}, 'Synthetic recovery',
                             consent_confirmed=True, capture_id=self.capture)
        queue.close()
        class UncertainAPI(RecordingAPI):
            fail_once = True
            def call(self, method, path, body=None):
                result = super().call(method, path, body)
                if path.endswith('/transcript') and self.fail_once:
                    self.fail_once = False
                    raise ValueError('api_unavailable')
                return result
        api = UncertainAPI()
        original_checkpoint = DesktopQueue.checkpoint
        interrupted = False
        def checkpoint(queue, identifier, token, stage, descriptor, **kwargs):
            nonlocal interrupted
            if stage == 'asr' and not interrupted:
                interrupted = True
                raise ValueError('synthetic_adoption_interrupted')
            return original_checkpoint(queue, identifier, token, stage, descriptor, **kwargs)
        def forbidden(*args, **kwargs):
            self.fail('Recovery repeated completed live model work')
        with patch.object(DesktopQueue, 'checkpoint', new=checkpoint), \
             patch('synth.worker.desktop_pipeline.normalize_audio', side_effect=forbidden), \
             patch('synth.worker.desktop_pipeline.prepare_acoustic_sources', side_effect=forbidden), \
             patch('synth.worker.desktop_pipeline.transcribe', side_effect=forbidden), \
             patch('synth.worker.desktop_pipeline.infer', return_value={'segments': [], 'speech_state': 'unknown'}), \
             patch('synth.speakers.matching.identify', return_value={'state': 'unavailable', 'matches': {}}), \
             patch('synth.speakers.matching.profile_signature', return_value=(None, None)):
            first = work_once(state/'queue.sqlite3', self.root, state, api)
            self.assertEqual(first['error_code'], 'synthetic_adoption_interrupted')
            self.assertIn('consolidated', first['checkpoints'])
            self.assertNotIn('asr', first['checkpoints'])
            self.assertTrue(list(self.root.rglob('*.wav')))
            for expected in ['api_unavailable', None]:
                queue = DesktopQueue(state/'queue.sqlite3', self.root)
                queue.retry(task['id']); queue.close()
                result = work_once(state/'queue.sqlite3', self.root, state, api)
                if expected:
                    self.assertEqual(result['error_code'], expected)
                else:
                    self.assertEqual(result['state'], 'succeeded')
                self.assertFalse(list(self.root.rglob('*.wav')))
        self.assertEqual(len(api.uploads), 2)
        self.assertEqual(api.uploads[0], api.uploads[1])
