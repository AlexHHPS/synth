import unittest
from unittest.mock import patch
from uuid import uuid4

from synth.speakers import sync
from synth.worker import desktop_pipeline


class VoiceSyncTests(unittest.TestCase):
    def setUp(self):
        self.actor = {'id': str(uuid4()), 'name': 'Synthetic owner'}
        self.profile = {'id': str(uuid4()), 'employee_id': self.actor['id'], 'name': self.actor['name'],
                        'model': 'test-model', 'updated_at': 'test', 'shared': False, 'vector': [1.0, 0.0]}
        owner = self.actor['id']
        class API:
            auth_mode = 'supabase'
            expected_principal = None
            current = owner
            writes = 0
            def assert_current_principal(self):
                if self.expected_principal != self.current:raise ValueError('auth_identity_changed')
            def call(self, method, path, body=None):
                self.assert_current_principal()
                if method != 'GET':self.writes += 1
                return {'profiles': []}
        self.api = API()
        self.addCleanup(patch.stopall)
        patch.object(desktop_pipeline, 'LocalAPI', return_value=self.api).start()
        patch.object(sync, 'local_profiles', return_value=[self.profile]).start()

    def test_share_cannot_send_previous_accounts_embedding_as_current_user(self):
        self.api.current = str(uuid4())
        with self.assertRaisesRegex(ValueError, 'auth_identity_changed'):
            sync.share(self.actor, self.profile['id'], True)
        self.assertEqual(self.api.writes, 0)

    def test_catalog_cannot_mix_another_accounts_corporate_profiles(self):
        self.api.current = str(uuid4())
        profiles, _, state = sync.catalog(self.actor)
        self.assertEqual(state, 'sync_unavailable')
        self.assertEqual(profiles, [self.profile])

    def test_unshare_rejects_an_account_switch(self):
        self.api.current = str(uuid4())
        with self.assertRaisesRegex(ValueError, 'auth_identity_changed'):
            sync.unshare(self.actor, self.profile['id'])
        self.assertEqual(self.api.writes, 0)

    def test_same_account_can_share_and_sync(self):
        sync.share(self.actor, self.profile['id'], True)
        self.assertEqual(self.api.writes, 1)
        self.assertEqual(sync.catalog(self.actor)[2], 'synced')
