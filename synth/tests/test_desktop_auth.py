import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
import time
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.parse import urlsplit, parse_qs

from synth.server import desktop_auth as auth

CONFIG={"mode":"supabase","supabase_url":"https://exampleproject.supabase.co","publishable_key":"sb_publishable_test","allowed_domains":["example.com"]}


class DesktopAuthTests(unittest.TestCase):
    def setUp(self):
        settings=patch.multiple(auth,SUPABASE="https://exampleproject.supabase.co",API="https://voice.example.com",ACCOUNT="test-project")
        settings.start(); self.addCleanup(settings.stop)
    def tearDown(self): auth.FLOW=None

    def test_capture_identity_is_offline_and_reflects_logout_and_account_switch(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(auth, 'APPDATA', Path(directory)), \
             patch.object(auth, 'fetch', side_effect=AssertionError('Capture identity must stay offline')):
            path = Path(directory)/'backend-config.json'
            path.write_text(json.dumps({'auth_mode': 'keys'}))
            with patch.object(auth, 'read_session') as read:
                self.assertEqual(auth.capture_identity(), {'mode': 'keys', 'signed_in': True})
                read.assert_not_called()
            path.write_text(json.dumps({'auth_mode': 'supabase'}))
            for principal in ['user-a', 'user-b', None]:
                session = {'principal_id': principal, 'access_token': 'must-not-escape'} if principal else None
                with patch.object(auth, 'read_session', return_value=session):
                    value = auth.capture_identity()
                    self.assertEqual(value['principal_id'], principal)
                    self.assertEqual(value['signed_in'], principal is not None)
                    self.assertNotIn('access_token', value)

    def test_browser_login_uses_pkce_and_random_loopback_callback(self):
        with patch.object(auth,"configuration",return_value=CONFIG),patch.object(auth.subprocess,"run") as launch:
            auth.begin()
        arguments=launch.call_args.args[0]
        self.assertEqual(arguments[0],"/usr/bin/open")
        url=urlsplit(arguments[1]);query=parse_qs(url.query)
        self.assertEqual(url.netloc,"exampleproject.supabase.co")
        self.assertEqual(query['provider'],['google'])
        expected=base64.urlsafe_b64encode(hashlib.sha256(auth.FLOW['verifier'].encode()).digest()).decode().rstrip('=')
        self.assertEqual(query['code_challenge'],[expected])
        self.assertNotIn(auth.FLOW['verifier'],arguments[1])
        self.assertEqual(query['redirect_to'],['http://127.0.0.1:18383/auth/callback/'+auth.FLOW['nonce']])

    def test_unknown_expired_and_replayed_callbacks_do_not_exchange_tokens(self):
        auth.FLOW={"nonce":"expected","verifier":"secret","expires_at":0,"state":"waiting"}
        with patch.object(auth,"fetch") as fetch:
            for path in ['/auth/callback/forged?code=x','/auth/callback/expected?code=x']:
                with self.assertRaises(ValueError): auth.callback(path)
            fetch.assert_not_called()

    def test_keychain_secrets_are_passed_via_stdin_and_never_argv(self):
        with patch.object(auth.subprocess,"run",return_value=MagicMock(returncode=0)) as command:
            auth.store_session({'access_token':'secret-value'})
        self.assertNotIn('secret-value',' '.join(command.call_args.args[0]))
        self.assertEqual(command.call_args.args[0],['/usr/bin/security','-i'])

    def test_rejects_unapproved_configuration_and_network_destinations(self):
        with patch.object(auth,"fetch",return_value={**CONFIG,'supabase_url':'https://evil.invalid'}):
            with self.assertRaisesRegex(ValueError,'auth_configuration_invalid'): auth.configuration()
        for url in ['https://evil.invalid/x',"https://exampleproject.supabase.co"+'.evil.invalid/auth/v1/user',auth.API+'@evil.invalid/x']:
            with self.assertRaisesRegex(ValueError,'auth_destination_denied'): auth.fetch(url)

    def test_local_api_rejects_session_switch_before_returning_statuses(self):
        from synth.worker.desktop_pipeline import LocalAPI
        api = LocalAPI.__new__(LocalAPI)
        api.auth_mode = 'supabase'; api.expected_principal = 'first-user'
        with patch.object(auth, 'access', return_value={'principal_id': 'first-user'}):
            api.assert_current_principal()
        with patch.object(auth, 'access', return_value={'principal_id': 'second-user'}):
            with self.assertRaisesRegex(ValueError, 'auth_identity_changed'):
                api.assert_current_principal()

    def test_pending_callback_cannot_restore_session_after_logout_or_replace_new_login(self):
        @contextmanager
        def isolated_session_lock():
            with auth.GATE:
                yield
        for replacement in (None, {'state': 'waiting', 'nonce': 'new-login'}):
            with self.subTest(replacement=replacement):
                auth.FLOW = {'state': 'waiting', 'nonce': 'old-login', 'verifier': 'test',
                             'expires_at': time.time()+300, 'config': CONFIG}
                started, finish = threading.Event(), threading.Event()
                def exchange(*args, **kwargs):
                    started.set()
                    if not finish.wait(3):raise AssertionError('Test exchange timed out')
                    return {'access_token': 'synthetic', 'refresh_token': 'synthetic'}
                with patch.object(auth, 'session_lock', isolated_session_lock), \
                     patch.object(auth, 'fetch', side_effect=exchange), \
                     patch.object(auth, 'read_session', return_value=None), \
                     patch.object(auth, 'accept_tokens') as accept, ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(auth.callback, '/auth/callback/old-login?code=test')
                    try:
                        self.assertTrue(started.wait(3))
                        if replacement is None:
                            self.assertEqual(auth.logout(), {'signed_in': False})
                        else:
                            with auth.GATE:auth.FLOW = replacement
                    finally:
                        finish.set()
                    with self.assertRaisesRegex(ValueError, 'auth_callback_invalid'):
                        pending.result(timeout=3)
                    accept.assert_not_called()
                    self.assertIs(auth.FLOW, replacement)

    def test_calendar_consent_uses_existing_client_and_additional_read_scope(self):
        with patch.object(auth, 'configuration', return_value=CONFIG), patch.object(auth, 'access', return_value={'principal_id':'alice','access_token':'synthetic'}), patch.object(auth,'fetch',return_value={'configured':True}), patch.object(auth.subprocess, 'run') as launch:
            auth.begin(calendar=True)
            from urllib.parse import urlsplit, parse_qs
            query = parse_qs(urlsplit(launch.call_args.args[0][1]).query)
            self.assertIn('calendar.events.readonly', query['scopes'][0])
            self.assertEqual(query['access_type'], ['offline'])
            self.assertIn('consent', query['prompt'][0])
            self.assertEqual(auth.FLOW['principal_id'], 'alice')

    def test_calendar_callback_cannot_switch_the_recorder(self):
        import time
        auth.FLOW={'nonce':'nonce','verifier':'secret','expires_at':time.time()+60,'state':'waiting','config':CONFIG,'calendar':True,'principal_id':'alice'}
        with tempfile.TemporaryDirectory() as directory, patch.object(auth, 'APPDATA', Path(directory)), \
             patch.object(auth,'fetch',return_value={'access_token':'synthetic'}), patch.object(auth,'read_session',return_value={'principal_id':'bob'}), patch.object(auth,'accept_tokens') as accept:
            with self.assertRaisesRegex(ValueError,'auth_identity_changed'):
                auth.callback('/auth/callback/nonce?code=synthetic')
            accept.assert_not_called()

    def test_calendar_configuration_failure_does_not_open_another_google_consent(self):
        with patch.object(auth,'configuration',return_value=CONFIG), patch.object(auth,'access',return_value={'principal_id':'alice','access_token':'synthetic'}), patch.object(auth,'fetch',return_value={'configured':False}), patch.object(auth.subprocess,'run') as launch:
            with self.assertRaisesRegex(ValueError,'calendar_client_configuration_invalid'):
                auth.begin(calendar=True)
            launch.assert_not_called()

    def test_active_callback_still_accepts_tokens(self):
        auth.FLOW = {'state': 'waiting', 'nonce': 'current', 'verifier': 'test',
                     'expires_at': time.time()+300, 'config': CONFIG}
        @contextmanager
        def isolated_session_lock():
            with auth.GATE:yield
        with patch.object(auth, 'session_lock', isolated_session_lock), \
             patch.object(auth, 'fetch', return_value={'access_token': 'synthetic'}), \
             patch.object(auth, 'accept_tokens') as accept:
            auth.callback('/auth/callback/current?code=test')
            accept.assert_called_once_with({'access_token': 'synthetic'}, CONFIG)
        self.assertEqual(auth.FLOW, {'state': 'signed_in', 'expires_at': auth.FLOW['expires_at'], 'error_code': None})


if __name__=='__main__': unittest.main()
