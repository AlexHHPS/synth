"""Live collector recovery and owner/catalog boundaries, using synthetic voices."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
import wave

import numpy as np
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from synth.worker.live_voice import LiveVoiceObserver, REVISION
from synth.worker.voice_binding import parse
from synth.speakers.live_status import capture_status, confirm_capture


def basis(size, index=0):
    return [float(i == index) for i in range(size)]


class SyntheticHelper:
    def __init__(self, binary, models, directory, revision, lock_fd):
        self.closed = False
        self.calls = []

    def diarize(self, path):
        self.calls.append('diarize')
        with wave.open(str(path)) as audio:
            duration = audio.getnframes()//16
        return {'segments': [{'speaker_id': 'S1', 'start_ms': 0, 'end_ms': duration}]}

    def embed_sealed(self, path, key, binding, output):
        self.calls.append('embed')
        metadata = parse(binding)
        nonce = os.urandom(12)
        payload = {'embedding': basis(256), 'rho128': basis(128, getattr(self, 'rho_index', 0)),
                   'revision': REVISION, 'duration_ms': metadata['crop_seconds']*1000}
        combined = nonce+AESGCM(key).encrypt(nonce, json.dumps(payload).encode(), binding.encode())
        output.write_text(json.dumps({'version': 1, 'binding': binding, 'combined': base64.b64encode(combined).decode()}))
        return {'path': str(output), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}

    def close(self):
        self.closed = True


class LiveVoiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root/'pipeline'
        self.capture_root = self.root/'capture'
        self.folder = self.capture_root/'recording'
        self.folder.mkdir(parents=True)
        self.owner, self.capture_id = str(uuid4()), str(uuid4())
        self.owner_path = self.folder/'capture-owner.json'
        self.owner_path.write_text(json.dumps({'version': 1, 'capture_id': self.capture_id, 'identity': self.owner}))
        self.model = 'a'*40
        self.lock = self.root/'models.json'
        self.lock.write_text(json.dumps({'model_revision': self.model}))
        self.binary = self.root/'native'; self.binary.write_text('synthetic helper')
        self.key = os.urandom(32)
        self.helpers = []
        self.rho_index = 0
        self.profiles = [{'id': 'enrolled-profile', 'employee_id': 'person-a', 'name': 'Persona A',
                          'model': self.model, 'vector': basis(256)}]

    def observer(self, **kwargs):
        def factory(*args, **kwargs):
            helper = SyntheticHelper(*args, **kwargs)
            helper.rho_index = self.rho_index
            self.helpers.append(helper)
            return helper
        value = LiveVoiceObserver(self.folder, self.capture_id, self.state, {}, 71,
            assets=lambda _: (self.binary, self.root, self.model, hashlib.sha256(self.lock.read_bytes()).hexdigest()),
            key_provider=lambda: self.key, factory=factory, **kwargs)
        self.addCleanup(value.close)
        return value

    def feed(self, observer, start, end, sources=('system',)):
        for second in range(start, end, 3):
            count = min(3, end-second)
            observer({s: np.ones(count*16000, dtype=np.float32)*.2 for s in sources},
                     second*48000, (second+count)*48000)

    def status(self, profiles=None, identity=None):
        return capture_status(self.capture_id, identity or self.owner, {'id': self.owner}, self.state, self.capture_root,
            snapshot=(self.profiles if profiles is None else profiles, 'current-catalog', 'synced'), key=self.key,
            project=lambda vectors, _: [basis(128) for _ in vectors], model_lock=self.lock)

    def confirm(self, body, guard=lambda: None):
        return confirm_capture(self.capture_id, self.owner, {'id': self.owner}, self.state, self.capture_root,
            body, snapshot=(self.profiles, 'current-catalog', 'synced'), key=self.key,
            project=lambda vectors, _: [basis(128) for _ in vectors], model_lock=self.lock, guard=guard)

    def decision(self, group=0, action='confirm', revision=0):
        match = self.status()['matches'][group]
        return {'decision_id': str(uuid4()), 'group_id': match['group_id'], 'action': action,
            'profile_id': self.profiles[0]['id'] if action == 'confirm' else None,
            'evidence_ids': match['evidence_ids'], 'expected_revision': revision, 'confirmed_by_user': True}

    def test_human_anchor_improves_later_blocks_without_automatically_confirming_them(self):
        self.profiles[0]['id'] = str(uuid4())
        self.rho_index = 2
        observer = self.observer(); self.feed(observer, 0, 30)
        self.assertEqual(self.status()['matches'][0]['state'], 'unknown')
        confirmed = self.confirm(self.decision())
        self.assertTrue(confirmed['confirmed'])
        self.assertEqual(confirmed['matches'][0]['state'], 'confirmed')
        self.feed(observer, 30, 60)
        result = self.status()
        self.assertEqual([r['state'] for r in result['matches']], ['confirmed', 'suggested'])
        self.assertIsNone(result['matches'][1]['employee_id'])
        self.assertEqual(result['matches'][1]['reference_evidence_ids'], result['matches'][0]['evidence_ids'])
        self.assertEqual(len(self.profiles), 1)

    def test_disagreeing_crop_policy_stays_unknown_in_public_status(self):
        observer=self.observer(); self.feed(observer,0,30)
        conflicting={**self.profiles[0],'id':'other-profile','employee_id':'person-b',
                     'model':self.model+'#crop10s'}
        result=self.status(profiles=[*self.profiles,conflicting])
        self.assertEqual(result['matches'][0]['state'],'unknown')
        self.assertEqual(result['matches'][0]['reason'],'crop_policy_disagreement')

    def test_later_human_confirmation_clarifies_earlier_blocks_and_undo_removes_derived_names(self):
        self.profiles[0]['id'] = str(uuid4()); self.rho_index = 2
        observer = self.observer(); self.feed(observer, 0, 60)
        result = self.confirm(self.decision(group=1))
        self.assertEqual([r['state'] for r in result['matches']], ['suggested', 'confirmed'])
        result = self.confirm(self.decision(group=1, action='clear', revision=1))
        self.assertEqual([r['state'] for r in result['matches']], ['unknown', 'unknown'])

    def test_phrase_crossing_two_confirmed_windows_keeps_identity_but_undo_or_revocation_removes_it(self):
        from synth.speakers.finalize_capture import map_confirmed
        self.profiles[0].update(id=str(uuid4()),employee_id=str(uuid4()))
        self.rho_index = 2
        observer = self.observer(); self.feed(observer,0,60)
        self.confirm(self.decision(group=0))
        confirmed = self.confirm(self.decision(group=1,revision=1))
        original = {'language':'es','duration_ms':60000,'model_fingerprint':'qwen:synthetic',
            'segments':[{'id':'s-boundary','text':'La decisión sigue pendiente.','start_ms':27000,'end_ms':33000,
                         'source_id':'system','speaker_id':'system:S1'}],
            'turns':[{'id':'t-boundary','text':'La decisión sigue pendiente.','start_ms':27000,'end_ms':33000,
                      'speaker_id':'system:S1','segment_ids':['s-boundary'],'word_count':4}]}
        mapped,actions = map_confirmed(original,confirmed)
        self.assertEqual(len(actions),1)
        self.assertEqual(actions[0]['employee_id'],self.profiles[0]['employee_id'])
        self.assertEqual(mapped['turns'][0]['segment_ids'],['s-boundary'])
        self.assertIsNone(mapped['segments'][0]['employee_id'])
        undone = self.confirm(self.decision(group=1,action='clear',revision=2))
        self.assertEqual(map_confirmed(original,undone),(original,[]))
        self.assertEqual(map_confirmed(original,self.status(profiles=[])),(original,[]))

    def test_confirmed_name_disappears_on_profile_revocation_and_survives_only_for_the_owner(self):
        self.profiles[0]['id'] = str(uuid4()); self.rho_index = 2
        observer = self.observer(); self.feed(observer, 0, 60)
        self.confirm(self.decision())
        self.assertTrue(all(r['candidate_name'] is None for r in self.status(profiles=[])['matches']))
        with self.assertRaisesRegex(ValueError, 'voice_capture_owner_mismatch'):
            self.status(identity=str(uuid4()))

    def test_lost_response_can_retry_but_stale_or_changed_decisions_cannot_overwrite_a_confirmation(self):
        self.profiles[0]['id'] = str(uuid4())
        observer = self.observer(); self.feed(observer, 0, 60)
        body = self.decision()
        self.assertEqual(self.confirm(body)['confirmation_revision'], 1)
        self.assertEqual(self.confirm(body)['confirmation_revision'], 1)
        with self.assertRaisesRegex(ValueError, 'voice_confirmation_revision_changed'):
            self.confirm(self.decision(group=1))
        with self.assertRaisesRegex(ValueError, 'voice_confirmation_id_reused'):
            self.confirm({**body, 'action': 'clear', 'profile_id': None})
        with self.assertRaisesRegex(ValueError, 'voice_confirmation_evidence_changed'):
            self.confirm({**self.decision(revision=1), 'evidence_ids': ['0'*64, '1'*64, '2'*64]})

    def test_simultaneous_confirmations_cannot_silently_replace_each_other(self):
        from concurrent.futures import ThreadPoolExecutor
        self.profiles[0]['id'] = str(uuid4())
        observer = self.observer(); self.feed(observer, 0, 60)
        bodies = [self.decision(group=index) for index in (0,1)]
        def commit(body):
            try:
                self.confirm(body)
                return 'saved'
            except ValueError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(commit, bodies))
        self.assertCountEqual(results, ['saved', 'voice_confirmation_revision_changed'])
        result = self.status()
        self.assertEqual(result['confirmation_revision'], 1)
        self.assertEqual(sum(r['confirmed'] for r in result['matches']), 1)

    def test_closing_freezes_the_acknowledged_human_decisions(self):
        from synth.speakers.capture_decisions import CaptureDecisions
        self.profiles[0]['id'] = str(uuid4())
        observer = self.observer(); self.feed(observer,0,30)
        self.confirm(self.decision())
        with CaptureDecisions(self.state,self.capture_id,self.owner) as store:
            revision, rows = store.seal()
        self.assertEqual(revision,1)
        self.assertEqual(len(rows),1)
        with self.assertRaisesRegex(ValueError,'voice_capture_finalized'):
            self.confirm(self.decision(action='clear',revision=1))

    def test_confirmation_requires_explicit_human_action_current_profile_and_current_principal(self):
        self.profiles[0]['id'] = str(uuid4())
        observer = self.observer(); self.feed(observer, 0, 30)
        body = self.decision()
        with self.assertRaisesRegex(ValueError, 'voice_confirmation_invalid'):
            self.confirm({**body, 'confirmed_by_user': False})
        with self.assertRaisesRegex(ValueError, 'voice_confirmation_profile_unavailable'):
            self.confirm({**body, 'profile_id': str(uuid4())})
        def revoked():
            raise ValueError('desktop_principal_changed')
        with self.assertRaisesRegex(ValueError, 'desktop_principal_changed'):
            self.confirm(body, guard=revoked)
        self.assertEqual(self.status()['confirmation_revision'], 0)

    def test_thirty_second_collection_emits_only_ciphertext_and_live_suggestions_use_current_catalog(self):
        observer = self.observer()
        self.feed(observer, 0, 27)
        self.assertEqual(self.helpers, [])
        self.feed(observer, 27, 30)
        self.assertEqual(len(self.helpers), 1)
        self.assertEqual(self.helpers[0].calls, ['diarize', 'embed', 'embed', 'embed'])
        result = self.status()
        self.assertEqual(result['matches'][0]['profile_id'], 'enrolled-profile')
        self.assertEqual(result['matches'][0]['candidate_name'], 'Persona A')
        self.assertFalse(result['matches'][0]['confirmed'])
        encoded = json.dumps(result)
        self.assertNotIn('"rho128":', encoded)
        self.assertNotIn('"embedding":', encoded)
        self.assertIsNone(result['matches'][0]['employee_id'])
        self.assertIsNone(self.status(profiles=[])['matches'][0]['candidate_name'])
        self.assertFalse(list((self.folder/'voice-context-v1').rglob('*.wav')))
        records = list((self.folder/'voice-context-v1').rglob('*.json'))
        self.assertTrue(all('embedding' not in p.read_text() for p in records))

    def test_restart_reuses_completed_crops_and_window_instead_of_repeating_inference(self):
        observer = self.observer()
        self.feed(observer, 0, 30)
        observer.close()
        replay = self.observer()
        self.feed(replay, 0, 30)
        self.assertEqual(self.helpers[-1].calls, [])
        self.assertEqual(replay.reused_windows, 1)
        self.assertEqual(self.status()['matches'][0]['candidate_employee_id'], 'person-a')

    def test_partial_window_at_close_and_both_sources_are_processed_by_one_helper(self):
        observer = self.observer()
        self.feed(observer, 0, 45, sources=('microphone', 'system'))
        observer.finish()
        self.assertEqual(observer.state, 'drained')
        self.assertEqual(len(self.helpers), 1)
        self.assertEqual(observer.diarization_calls, 4)
        self.assertEqual(observer.embedding_calls, 12)
        result = self.status()
        self.assertEqual(len(result['matches']), 4)
        self.assertEqual({r['source'] for r in result['matches']}, {'microphone', 'system'})

    def test_clock_and_owner_failures_abstain_without_raising_into_asr(self):
        observer = self.observer()
        observer({'system': np.ones(16000)}, 48000, 96000)
        self.assertEqual(observer.state, 'unavailable')
        self.assertEqual(observer.error_code, 'voice_capture_clock_invalid')
        observer = self.observer()
        self.owner_path.write_text(json.dumps({'version': 1, 'capture_id': self.capture_id, 'identity': str(uuid4())}))
        self.feed(observer, 0, 30)
        self.assertEqual(observer.error_code, 'voice_capture_owner_changed')
        self.assertEqual(self.helpers, [])

    def test_other_principal_changed_ciphertext_and_changed_audio_are_rejected(self):
        observer = self.observer()
        self.feed(observer, 0, 30)
        with self.assertRaisesRegex(ValueError, 'voice_capture_owner_mismatch'):
            self.status(identity=str(uuid4()))
        sealed = next((self.folder/'voice-context-v1').rglob('*.sealed.json'))
        sealed.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'voice_observation_file_changed'):
            self.status()
        observer.close()
        replay = self.observer()
        for second in range(0, 30, 3):
            replay({'system': np.ones(48000, dtype=np.float32)*.3}, second*48000, (second+3)*48000)
        self.assertEqual(replay.error_code, 'voice_capture_input_changed')

    def test_keychain_or_helper_failure_is_optional_and_does_not_repeat_each_slab(self):
        observer = self.observer()
        def denied():
            raise ValueError('voice_keychain_unavailable')
        observer.key_provider = denied
        self.feed(observer, 0, 30)
        self.assertEqual(observer.error_code, 'voice_keychain_unavailable')
        self.feed(observer, 30, 60)
        self.assertEqual(len(self.helpers), 0)
        self.assertEqual(observer.buffers, {})
