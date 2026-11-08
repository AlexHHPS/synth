"""Synthetic vector tests of policy separation, never acoustic accuracy."""
from array import array
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from synth.speakers import matching
from synth.speakers.prints import MeetingPrints, RETENTION_SECONDS

P = [1.] + [0.] * 255
Q = [0., 1.] + [0.] * 254


class PolicyCompatibilityTests(unittest.TestCase):
    def test_two_policies_disagreeing_cannot_name_a_speaker(self):
        a = {'active_profiles': 1, 'matches': {'system:S1': {'state': 'suggested', 'candidate_employee_id': 'a'}}}
        b = {'active_profiles': 1, 'matches': {'system:S1': {'state': 'candidate', 'candidate_employee_id': 'b'}}}
        result = matching._combine_policies(a, b)
        self.assertEqual(result['matches']['system:S1']['state'], 'unknown')
        self.assertIsNone(result['matches']['system:S1']['candidate_employee_id'])
        self.assertEqual(result['matches']['system:S1']['reason'], 'crop_policy_disagreement')

    def test_agreement_stays_a_suggestion_without_merging_incompatible_scores(self):
        a = {'active_profiles': 1, 'matches': {'system:S1': {'state': 'suggested', 'candidate_employee_id': 'a', 'similarity': .8}}}
        b = {'active_profiles': 1, 'matches': {'system:S1': {'state': 'candidate', 'candidate_employee_id': 'a', 'similarity': .95}}}
        result = matching._combine_policies(a, b)
        self.assertEqual(result['matches']['system:S1']['state'], 'suggested')
        self.assertEqual(result['matches']['system:S1']['similarity'], .8)
        self.assertEqual(len(result['matches']['system:S1']['policy_evidence']), 2)

    def test_existing_bare_profiles_use_five_second_votes_and_reidentify_after_audio_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root/'model-lock.json'; lock.write_text(json.dumps({'model_revision': 'current'}))
            audio = root/'public.wav'; audio.write_bytes(b'synthetic placeholder')
            path, key = root/'prints.sqlite3', os.urandom(32)
            profiles = [{'id': 'p', 'employee_id': 'a', 'name': 'Synthetic A', 'model': 'current', 'vector': P}]
            with patch('synth.speakers.native.LOCK', lock), \
                 patch('synth.speakers.sync.catalog', return_value=(profiles, 'first', 'fresh')), \
                 patch('synth.speakers.prints.open_prints', lambda: MeetingPrints(path, key)), \
                 patch('synth.speakers.matching_legacy.configured_space', return_value='cosine256'), \
                 patch('synth.speakers.matching.configured_space', return_value='cosine256'), \
                 patch('synth.speakers.matching_legacy.samples', return_value=array('h', [0])*320000), \
                 patch('synth.speakers.matching_legacy.embed_chunk', return_value=P) as embed, \
                 patch('synth.speakers.matching.cluster_prints', side_effect=AssertionError('Cannot compare legacy profiles with 10s crops')):
                from synth.worker.journal import file_hash
                result = matching.identify({'id': 'a'}, {'system': {'path': str(audio), 'sha256': file_hash(audio)}},
                    {'system': {'segments': [{'start_ms': 0, 'end_ms': 20000, 'speaker_id': 'S1'}]}}, root, task_id='task')
                self.assertEqual(result['matches']['system:S1']['state'], 'suggested')
                self.assertTrue(all(len(call.args[0]) == 5*16000 for call in embed.call_args_list))
                self.assertNotIn('vector', json.dumps(result))
                audio.unlink()
                later = matching.reidentify({'id': 'a'}, 'task')
                self.assertEqual(later['matches']['system:S1']['candidate_employee_id'], 'a')
                profiles[0] = {**profiles[0], 'employee_id': 'b', 'vector': Q}
                revoked = matching.reidentify({'id': 'a'}, 'task')
                self.assertEqual(revoked['matches']['system:S1']['state'], 'unknown')
                self.assertIsNone(revoked['matches']['system:S1']['candidate_employee_id'])


class WindowRetentionTests(unittest.TestCase):
    def test_windows_and_centroid_coexist_encrypted_and_forget_erases_both(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'prints.sqlite3'
            store = MeetingPrints(path, os.urandom(32))
            try:
                store.store('task', 'system:S1', 'm#crop10s', P, 30.)
                store.store_windows('task', 'system:S1', 'm', [P, P], 10., 'legacy-policy', [{'state': 'valid'}])
                self.assertIn('system:S1', store.load('task', 'm#crop10s'))
                self.assertEqual(store.load_windows('task', 'm', 'legacy-policy')['system:S1']['vectors'], [P, P])
                self.assertEqual(store.load_windows('task', 'm', 'other-policy'), {})
                self.assertNotIn(json.dumps(P).encode()[:40], path.read_bytes())
                self.assertEqual(store.forget('task', 'system:S1'), 2)
                self.assertEqual(store.load_windows('task', 'm', 'legacy-policy'), {})
            finally:
                store.close()

    def test_window_expiry_and_authenticated_meeting_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1000.]
            store = MeetingPrints(Path(directory)/'prints.sqlite3', os.urandom(32), clock=lambda: now[0])
            try:
                store.store_windows('task', 'system:S1', 'm', [P, P], 10., 'policy', [])
                now[0] += RETENTION_SECONDS+1
                self.assertEqual(store.load_windows('task', 'm', 'policy'), {})
                store.store_windows('task', 'system:S1', 'm', [P, P], 10., 'policy', [])
                store.db.execute("UPDATE window_prints SET task_id='other'")
                with self.assertRaises(Exception):
                    store.load_windows('other', 'm', 'policy')
            finally:
                store.close()
