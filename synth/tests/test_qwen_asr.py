import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from synth.worker import asr, qwen_asr
from synth.worker.qwen_runner import align_chunk, audio_ranges, decode_chunks, group_words, restore_punctuation


class Processor:
    def tokenize_space_lang(self, text):
        return [re.sub(r'[^\w]', '', word) for word in text.split() if re.sub(r'[^\w]', '', word)]


class QwenTests(unittest.TestCase):
    @staticmethod
    def samples(seconds):
        import numpy as np
        return np.broadcast_to(np.float32(.1), round(seconds*16000))

    @staticmethod
    def decoded(text='Texto literal.', tokens=100):
        return SimpleNamespace(text=text, segments=[{'text': text}], generation_tokens=tokens)

    def test_long_recordings_cover_all_frames_with_independent_budgets(self):
        for minutes in (3, 20, 60):
            with self.subTest(minutes=minutes):
                samples = self.samples(minutes*60)
                ranges = list(audio_ranges(samples))
                self.assertEqual(ranges[0][0], 0)
                self.assertEqual(ranges[-1][1], len(samples))
                self.assertTrue(all(a[1] == b[0] for a,b in zip(ranges, ranges[1:])))
                calls = []
                def generate(clip, **kwargs):
                    calls.append((len(clip), kwargs['max_tokens']))
                    self.assertLessEqual(len(clip), 30*16000)
                    return self.decoded(tokens=100)
                result = decode_chunks(samples, generate)
                self.assertEqual(len(result['chunks']), len(ranges))
                self.assertEqual(result['generation_tokens'], 100*len(calls))
                self.assertGreater(result['generation_tokens'], max(budget for _,budget in calls))
                self.assertEqual(result['chunks'][-1]['end'], minutes*60)

    def test_interruption_resumes_only_missing_chunks_and_cache_key_fences_audio(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as t:
            cache = Path(t)/'decoded.json'
            interrupted = Mock(side_effect=[self.decoded(), RuntimeError('interrupted')])
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                decode_chunks(self.samples(80), interrupted, cache=cache, cache_key='audio:model')
            self.assertEqual(len(json.loads(cache.read_text())['chunks']), 1)
            resumed = Mock(return_value=self.decoded())
            result = decode_chunks(self.samples(80), resumed, cache=cache, cache_key='audio:model')
            self.assertEqual(resumed.call_count, len(result['chunks'])-1)
            fenced = Mock(return_value=self.decoded())
            decode_chunks(self.samples(80), fenced, cache=cache, cache_key='different:model')
            self.assertEqual(fenced.call_count, len(result['chunks']))

    def test_exhaustion_subdivides_only_bad_interval_and_resumes_split_tree(self):
        from unittest.mock import Mock
        def generate(clip, **kwargs):
            return self.decoded(tokens=kwargs['max_tokens'] if len(clip)>15*16000 else 100)
        with tempfile.TemporaryDirectory() as t:
            cache = Path(t)/'decoded.json'
            result = decode_chunks(self.samples(30), generate, cache=cache, cache_key='audio:model')
            self.assertEqual(len(result['chunks']), 2)
            self.assertEqual(result['generation_tokens'], 200)
            self.assertEqual([(c['start'],c['end']) for c in result['chunks']], [(0,15),(15,30)])
            forbidden = Mock(side_effect=AssertionError('cached audio decoded again'))
            self.assertEqual(decode_chunks(self.samples(30), forbidden, cache=cache, cache_key='audio:model'), result)

    def test_permanent_token_exhaustion_is_bounded_and_never_published(self):
        calls = []
        def generate(clip, **kwargs):
            calls.append(len(clip))
            return self.decoded(tokens=kwargs['max_tokens'])
        with self.assertRaisesRegex(ValueError, 'qwen_token_limit_reached'):
            decode_chunks(self.samples(30), generate)
        self.assertEqual(len(calls), 4)

    def test_silence_policy_keeps_cold_uncertainty_and_never_accepts_incomplete_output(self):
        from unittest.mock import Mock
        generate = Mock(return_value=self.decoded())
        presence = Mock(side_effect=[None, False, True])
        result = decode_chunks(self.samples(80), generate, presence=presence)
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(result['chunks'][1]['text'], '')
        self.assertIsNone(result['chunks'][0]['speech_presence'])
        bad = Mock(return_value=SimpleNamespace(text='No falta', segments=[{'text':'falta'}], generation_tokens=2))
        with self.assertRaisesRegex(ValueError, 'qwen_asr_incomplete'):
            decode_chunks(self.samples(1), bad)

    def test_token_limit_error_reaches_task_with_safe_code(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaisesRegex(ValueError, 'qwen_token_limit_reached'):
                self.run_adapter(Path(t), subprocess.CompletedProcess([], 1,
                    json.dumps({'error_code': 'qwen_token_limit_reached'}), 'private detail'))

    @patch.dict('sys.modules', {'numpy': SimpleNamespace(pad=lambda clip, width: clip + [0] * width[1])})
    def test_alignment_retries_boundary_prediction_without_relaxing_validation(self):
        from unittest.mock import Mock
        bad = SimpleNamespace(items=[SimpleNamespace(text='Hola', start_time=229.6, end_time=230.56)])
        good = SimpleNamespace(items=[SimpleNamespace(text='Hola', start_time=229.5, end_time=229.9)])
        aligner = SimpleNamespace(generate=Mock(side_effect=[bad, good]), aligner_processor=Processor())
        clip = [0] * round(229.95225 * 16000)
        words, retries = align_chunk(aligner, clip, 'Hola', 972)
        self.assertEqual(retries, 1)
        self.assertEqual(words, [{'text': 'Hola', 'start_ms': 1201500, 'end_ms': 1201900}])
        self.assertEqual(len(aligner.generate.call_args.args[0]), len(clip) + 16000)

    @patch.dict('sys.modules', {'numpy': SimpleNamespace(pad=lambda clip, width: clip + [0] * width[1])})
    def test_alignment_retry_cannot_publish_times_in_added_silence(self):
        from unittest.mock import Mock
        bad = SimpleNamespace(items=[SimpleNamespace(text='Hola', start_time=.5, end_time=1.8)])
        aligner = SimpleNamespace(generate=Mock(return_value=bad), aligner_processor=Processor())
        with self.assertRaisesRegex(ValueError, 'qwen_alignment_invalid_time'):
            align_chunk(aligner, [0] * 16000, 'Hola', 0)
        self.assertEqual(aligner.generate.call_count, 3)

    def test_alignment_can_recover_clipped_leading_context_with_original_clock(self):
        from unittest.mock import Mock
        bad = SimpleNamespace(items=[SimpleNamespace(text='Hola', start_time=.4, end_time=18.56)])
        good = SimpleNamespace(items=[SimpleNamespace(text='Hola', start_time=1.4, end_time=18.8)])
        aligner = SimpleNamespace(generate=Mock(side_effect=[bad, bad, good]), aligner_processor=Processor())
        words, retries = align_chunk(aligner, self.samples(18), 'Hola', 69)
        self.assertEqual(retries, 2)
        self.assertEqual(words, [{'text': 'Hola', 'start_ms': 69400, 'end_ms': 86800}])
        self.assertEqual(len(aligner.generate.call_args.args[0]), 20*16000)

    @patch.dict('sys.modules', {'numpy': SimpleNamespace(pad=lambda clip, width: clip + [0] * width[1])})
    def test_alignment_text_mismatch_is_never_repaired_or_retried(self):
        from unittest.mock import Mock
        bad = SimpleNamespace(items=[SimpleNamespace(text='Otro', start_time=0, end_time=.5)])
        aligner = SimpleNamespace(generate=Mock(return_value=bad), aligner_processor=Processor())
        with self.assertRaisesRegex(ValueError, 'qwen_alignment_text_mismatch'):
            align_chunk(aligner, [0] * 16000, 'Hola', 0)
        self.assertEqual(aligner.generate.call_count, 1)

    def test_runner_alignment_error_reaches_the_task_with_safe_code(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaisesRegex(ValueError, 'qwen_alignment_invalid_time'):
                self.run_adapter(Path(t), subprocess.CompletedProcess([], 1,
                    json.dumps({'error_code': 'qwen_alignment_invalid_time'}), 'private detail'))
            self.assertEqual(json.loads((Path(t)/'out/qwen-error.json').read_text()),
                             {'error_code': 'qwen_alignment_invalid_time'})

    def test_alignment_preserves_asr_punctuation_and_tokens(self):
        items = [SimpleNamespace(text='Hola', start_time=.1, end_time=.4),
                 SimpleNamespace(text='Synth', start_time=.5, end_time=.9)]
        words = restore_punctuation('¿Hola, Synth! …', items, Processor(), 20, 1)
        self.assertEqual(' '.join(w['text'] for w in words), '¿Hola, Synth! …')
        self.assertEqual(words[0]['start_ms'], 20100)
        self.assertEqual(words[-1]['end_ms'], 20900)

    def test_alignment_cannot_change_words_or_accept_invalid_times(self):
        for item in [SimpleNamespace(text='Otra', start_time=0, end_time=1),
                     SimpleNamespace(text='Hola', start_time=float('nan'), end_time=1),
                     SimpleNamespace(text='Hola', start_time=1, end_time=.5),
                     SimpleNamespace(text='Hola', start_time=0, end_time=3)]:
            with self.assertRaises(ValueError):
                restore_punctuation('Hola', [item], Processor(), 0, 1)

    def test_phrase_groups_preserve_words_and_speech_gaps(self):
        words = [{'text': 'Hola,', 'start_ms': 0, 'end_ms': 500},
                 {'text': 'equipo.', 'start_ms': 500, 'end_ms': 1000},
                 {'text': 'Revisamos', 'start_ms': 2000, 'end_ms': 2500},
                 {'text': 'Synth', 'start_ms': 2600, 'end_ms': 3000}]
        groups = group_words(words)
        self.assertEqual([g['text'] for g in groups], ['Hola, equipo.', 'Revisamos Synth'])
        self.assertEqual(groups[1]['start_ms'], 2000)

    def test_model_integrity_is_checked_before_inference(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); model = root/'weights'; model.write_bytes(b'original')
            lock = root/'lock.json'
            lock.write_text(json.dumps({'models': {'asr': {'files': [{'path': 'weights', 'size': 8,
                'sha256': hashlib.sha256(b'original').hexdigest()}]}}}))
            cfg = {'python': sys.executable, 'asr': str(root), 'aligner': str(root)}
            with patch.object(qwen_asr, 'LOCK', lock):
                self.assertTrue(qwen_asr.verify_runtime(cfg).startswith('qwen3-asr-1.7b-bf16+aligner:'))
                model.write_bytes(b'modified')
                with self.assertRaisesRegex(ValueError, 'qwen_model_hash_mismatch'):
                    qwen_asr.verify_runtime(cfg)

    def test_missing_or_invalid_runtime_is_rejected(self):
        for cfg in [{}, {'python': '/missing/python', 'asr': '/missing', 'aligner': '/missing'}]:
            with self.assertRaisesRegex(ValueError, 'qwen_runtime_'):
                qwen_asr.verify_runtime(cfg)

    def test_qwen_dispatch_does_not_load_whisper_model(self):
        with tempfile.TemporaryDirectory() as t:
            config = Path(t)/'config.json'; config.write_text('{"backend":"qwen3"}')
            with patch.object(qwen_asr, 'CONFIG', config), patch.object(qwen_asr, 'transcribe', return_value=('qwen', {})) as runner:
                result = asr.transcribe('/missing/audio', Path(t)/'out', source_id='system', lock_fd=12)
            self.assertEqual(result[0], 'qwen')
            self.assertEqual(runner.call_args.args[2:4], ('system', 12))

    def run_adapter(self, root, result):
        wav = root/'audio.wav'
        with wave.open(str(wav), 'wb') as out:
            out.setparams((1, 2, 16000, 0, 'NONE', 'not compressed')); out.writeframes(b'\0\0'*16000)
        cfg = {'python': sys.executable, 'asr': '/local/asr', 'aligner': '/local/aligner'}
        with patch.object(qwen_asr, 'verify_runtime', return_value='qwen:test'), \
             patch.object(qwen_asr, 'run_process', return_value=result) as process:
            value = qwen_asr.transcribe(wav, root/'out', 'microphone', lock_fd=12, configuration=cfg)
        return value, process.call_args

    def test_offline_runner_returns_exact_words_and_unknown_identity(self):
        raw = {'segments': [{'text': 'Texto literal.', 'start_ms': 100, 'end_ms': 800}], 'words': []}
        with tempfile.TemporaryDirectory() as t:
            (transcript, _), call = self.run_adapter(Path(t), subprocess.CompletedProcess([], 0, json.dumps(raw), ''))
        self.assertEqual(call.kwargs['env']['HF_HUB_OFFLINE'], '1')
        self.assertEqual(call.kwargs['pass_fds'], (12,))
        self.assertIn('-I', call.args[0])
        self.assertEqual(transcript['segments'][0]['text'], 'Texto literal.')
        self.assertIsNone(transcript['segments'][0]['employee_id'])

    def test_invalid_runner_timeline_cannot_be_published(self):
        raw = {'segments': [{'text': 'Texto', 'start_ms': 500, 'end_ms': 2000}]}
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError):
                self.run_adapter(Path(t), subprocess.CompletedProcess([], 0, json.dumps(raw), ''))


if __name__ == '__main__':
    unittest.main()
