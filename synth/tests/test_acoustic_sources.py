import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

from synth.worker.acoustic import prepare_acoustic_sources
from synth.worker.journal import file_hash


class AcousticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.normalized = {}
        for source in ['microphone', 'system']:
            path = self.root/(source+'.wav')
            with wave.open(str(path), 'wb') as stream:
                stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b'\0\0'*16000)
            self.normalized[source] = {'path': str(path), 'sha256': file_hash(path)}
        self.configuration = {'echo_cancellation': 'aec3', 'python': sys.executable}

    def test_disabled_and_single_source_keep_original_inputs(self):
        for configuration, sources in [({}, self.normalized), (self.configuration, {'microphone': self.normalized['microphone']})]:
            with patch('synth.worker.acoustic.run_process') as child:
                result = prepare_acoustic_sources(sources, self.root, configuration=configuration)
            self.assertEqual(result['sources'], sources)
            child.assert_not_called()

    def test_cancellation_is_a_new_private_file_and_keeps_reference_unchanged(self):
        def child(*args, **kwargs):
            request = json.loads(kwargs['input'])
            shutil.copyfile(request['microphone'], request['output'])
            self.assertEqual(kwargs['pass_fds'], (7,))
            return subprocess.CompletedProcess([], 0, '{"engine":"WebRTC-AEC3"}', '')
        with patch('synth.worker.acoustic.run_process', side_effect=child):
            result = prepare_acoustic_sources(self.normalized, self.root, lock_fd=7, configuration=self.configuration)
        self.assertEqual(result['sources']['system'], self.normalized['system'])
        self.assertNotEqual(result['sources']['microphone']['path'], self.normalized['microphone']['path'])
        self.assertEqual(Path(result['sources']['microphone']['path']).stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(file_hash(d['path']) == d['sha256'] for d in self.normalized.values()))

    def test_truncated_cancellation_is_rejected_and_originals_survive(self):
        def child(*args, **kwargs):
            target = json.loads(kwargs['input'])['output']
            with wave.open(target, 'wb') as stream:
                stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'));stream.writeframes(b'\0\0'*10)
            return subprocess.CompletedProcess([], 0, '{}', '')
        with patch('synth.worker.acoustic.run_process', side_effect=child):
            with self.assertRaisesRegex(ValueError, 'echo_output_invalid'):
                prepare_acoustic_sources(self.normalized, self.root, configuration=self.configuration)
        self.assertFalse((self.root/'microphone.aec.wav').exists())
        self.assertFalse((self.root/'microphone.aec.part.wav').exists())
        self.assertTrue(all(Path(d['path']).exists() for d in self.normalized.values()))

    def test_timeout_is_an_explicit_retryable_failure(self):
        with patch('synth.worker.acoustic.run_process', side_effect=subprocess.TimeoutExpired('aec', 1)):
            with self.assertRaisesRegex(ValueError, 'echo_processing_timeout'):
                prepare_acoustic_sources(self.normalized, self.root, configuration=self.configuration)
