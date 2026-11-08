"""Counterexamples for the product reference session; no acoustic accuracy claims."""
import unittest
from uuid import uuid4

from synth.speakers.reference_session import ReferenceSession


def basis(size, index=0):
    return [float(i == index) for i in range(size)]


class ReferenceSessionTests(unittest.TestCase):
    def setUp(self):
        self.model = 'a'*40
        self.profiles = [{'id': 'registered-a', 'employee_id': 'a', 'name': 'Persona A',
                          'model': self.model, 'vector': basis(128)}]
        self.capture, self.owner = str(uuid4()), str(uuid4())

    def group(self, index, rho=None, raw=None, source='system'):
        start = index*160000
        common = {'capture_id': self.capture, 'owner': self.owner, 'source': source,
                  'model_lock_sha256': 'b'*64, 'policy': 'guarded_disjoint_voice_windows_v1'}
        five = [{**common, 'id': f'{index}-{i}', 'model': self.model, 'crop_seconds': 5,
                 'ranges': [[start+i*80000,start+(i+1)*80000]], 'rho128': rho or basis(128, 2)} for i in range(2)]
        ten = {**common, 'id': str(index)+'-ten', 'model': self.model+'#crop10s', 'crop_seconds': 10,
               'ranges': [r for o in five for r in o['ranges']], 'embedding': raw or basis(256)}
        return [*five, ten]

    def test_direct_match_seeds_once_and_fallback_returns_registered_profile_not_session_id(self):
        session = ReferenceSession(self.profiles, self.model)
        first = session.observe('first', self.group(0, rho=basis(128)))
        later = session.observe('later', self.group(1))
        self.assertEqual(first['reason'], 'legacy_evidence')
        self.assertEqual(later['reason'], 'session_reference')
        self.assertEqual(later['profile_id'], 'registered-a')
        self.assertIsNone(later['employee_id'])
        self.assertFalse(later['confirmed'])
        self.assertEqual(later['reference_evidence_ids'], ['0-0', '0-1', '0-ten'])

    def test_no_initial_match_means_no_reference_and_no_name(self):
        session = ReferenceSession(self.profiles, self.model)
        for i in range(3):
            self.assertEqual(session.observe(str(i), self.group(i))['state'], 'unknown')
        self.assertEqual(session.references, {})

    def test_fallback_cannot_update_anchor_and_drift_towards_another_voice(self):
        session = ReferenceSession(self.profiles, self.model)
        session.observe('first', self.group(0, rho=basis(128)))
        middle = [.8,.6]+[0.]*254
        last = [.3,(1-.3**2)**.5]+[0.]*254
        self.assertEqual(session.observe('middle', self.group(1, raw=middle))['state'], 'suggested')
        self.assertEqual(session.observe('last', self.group(2, raw=last))['state'], 'unknown')
        self.assertEqual(session.references['a']['vector'], basis(256))

    def test_echo_or_same_audio_in_another_channel_cannot_match_its_own_anchor(self):
        session = ReferenceSession(self.profiles, self.model)
        session.observe('original', self.group(0, rho=basis(128)))
        self.assertEqual(session.observe('echo', self.group(0, source='microphone'))['state'], 'unknown')

    def test_overlapping_five_second_crops_cannot_supply_two_votes(self):
        session = ReferenceSession(self.profiles, self.model)
        group = self.group(0, rho=basis(128))
        group[1]['ranges'] = group[0]['ranges']
        self.assertEqual(session.observe('duplicated', group)['state'], 'unknown')

    def test_joined_ten_seconds_must_have_exact_same_audio_lineage(self):
        session = ReferenceSession(self.profiles, self.model)
        group = self.group(0, rho=basis(128))
        group[2]['ranges'] = [[160000,320000]]
        self.assertEqual(session.observe('first', group)['state'], 'suggested')
        self.assertEqual(session.references, {})
        self.assertEqual(session.observe('next', self.group(1))['state'], 'unknown')

    def test_conflicting_strong_profile_vote_blocks_fallback(self):
        profiles = [*self.profiles, {'id': 'registered-b', 'employee_id': 'b', 'name': 'Persona B',
                                   'model': self.model, 'vector': basis(128, 1)}]
        session = ReferenceSession(profiles, self.model)
        session.observe('first', self.group(0, rho=basis(128)))
        group = self.group(1)
        group[0]['rho128'] = basis(128, 1)
        self.assertEqual(session.observe('mixed', group)['state'], 'unknown')

    def test_current_catalog_and_capture_boundaries_prevent_old_names_surviving_revocation(self):
        session = ReferenceSession(self.profiles, self.model)
        session.observe('first', self.group(0, rho=basis(128)))
        revoked = ReferenceSession([], self.model)
        self.assertEqual(revoked.observe('first', self.group(0, rho=basis(128)))['state'], 'unknown')
        self.assertEqual(revoked.observe('next', self.group(1))['state'], 'unknown')
        self.capture = str(uuid4())
        with self.assertRaisesRegex(ValueError, 'voice_session_capture_mismatch'):
            session.observe('foreign', self.group(2))
