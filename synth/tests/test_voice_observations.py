"""Authenticated provenance and independent source ranges, synthetic vectors only."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from synth.speakers.native import REVISION
from synth.speakers.observations import load_observation
from synth.worker.voice_binding import binding, parse, overlap


class VoiceObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.lock = self.root/'model.json'
        self.lock.write_text(json.dumps({'model_revision': 'a'*40}))
        self.arguments = {'capture_id': str(uuid4()), 'owner': str(uuid4()), 'source': 'system',
            'model': 'a'*40, 'model_lock_sha256': hashlib.sha256(self.lock.read_bytes()).hexdigest(),
            'crop_seconds': 5, 'ranges': [[0, 80000]], 'input_sha256': 'b'*64}
        self.key = os.urandom(32)
        self.payload = {'embedding': [1.]+[0.]*255, 'rho128': [1.]+[0.]*127,
                        'revision': REVISION, 'duration_ms': 5000}

    def envelope(self, payload=None):
        text = binding(**self.arguments)
        nonce = os.urandom(12)
        combined = nonce+AESGCM(self.key).encrypt(nonce, json.dumps(payload or self.payload).encode(), text.encode())
        path = self.root/(str(uuid4())+'.json')
        path.write_text(json.dumps({'version': 1, 'binding': text, 'combined': base64.b64encode(combined).decode()}))
        return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}, text

    def load(self, descriptor, text, key=None):
        return load_observation(descriptor, text, self.root, self.key if key is None else key, model_lock=self.lock)

    def test_bound_owner_capture_channel_and_policy_cannot_be_substituted(self):
        descriptor, text = self.envelope()
        value = self.load(descriptor, text)
        self.assertEqual(value['ranges'], [[0, 80000]])
        self.assertEqual(value['embedding'], self.payload['embedding'])
        for fields in [{'owner': str(uuid4())}, {'capture_id': str(uuid4())}, {'source': 'microphone'},
                       {'crop_seconds': 10, 'model': 'a'*40+'#crop10s', 'ranges': [[0, 160000]]}]:
            with self.subTest(fields=tuple(fields)), self.assertRaisesRegex(ValueError, 'voice_observation_authentication_failed'):
                self.load(descriptor, binding(**{**self.arguments, **fields}))

    def test_changed_file_wrong_key_and_model_revision_are_rejected(self):
        descriptor, text = self.envelope()
        with self.assertRaisesRegex(ValueError, 'voice_observation_authentication_failed'):
            self.load(descriptor, text, os.urandom(32))
        self.lock.write_text(json.dumps({'model_revision': 'c'*40}))
        with self.assertRaisesRegex(ValueError, 'voice_observation_model_mismatch'):
            self.load(descriptor, text)
        self.lock.write_text(json.dumps({'model_revision': 'a'*40}))
        Path(descriptor['path']).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'voice_observation_file_changed'):
            self.load(descriptor, text)

    def test_non_unit_wrong_dimension_or_wrong_duration_does_not_become_a_valid_print(self):
        for fields in [{'embedding': [2.]+[0.]*255}, {'rho128': [1.]}, {'duration_ms': 10000}]:
            with self.subTest(fields=tuple(fields)), self.assertRaisesRegex(ValueError, 'voice_observation_payload_invalid'):
                self.load(*self.envelope({**self.payload, **fields}))

    def test_symlinked_observation_cannot_escape_its_capture(self):
        descriptor, text = self.envelope()
        link = self.root/'alias.json'; link.symlink_to(descriptor['path'])
        with self.assertRaisesRegex(ValueError, 'voice_observation_path_denied'):
            self.load({**descriptor, 'path': str(link)}, text)

    def test_exact_sample_ranges_are_disjoint_and_an_echo_is_not_an_independent_vote(self):
        text = binding(**{**self.arguments, 'ranges': [[0, 40000], [80000, 120000]]})
        self.assertEqual(parse(text)['clock'], 'recording_elapsed')
        for ranges in ([[0, 80000], [40000, 120000]], [[True, 80001]], [[0, 79999]], [[-1, 79999]]):
            with self.subTest(ranges=ranges), self.assertRaisesRegex(ValueError, 'voice_observation_ranges_invalid'):
                binding(**{**self.arguments, 'ranges': ranges})
        self.assertTrue(overlap([[0, 40000], [80000, 120000]], [[90000, 130000]]))
        self.assertFalse(overlap([[0, 80000]], [[80000, 160000]]))
