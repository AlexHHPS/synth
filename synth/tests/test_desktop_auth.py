import base64
import hashlib
import unittest
from unittest.mock import patch, MagicMock
from urllib.parse import urlsplit, parse_qs

from synth.server import desktop_auth as auth

CONFIG={"mode":"supabase","supabase_url":"https://exampleproject.supabase.co","publishable_key":"sb_publishable_test","allowed_domains":["example.com"]}


class DesktopAuthTests(unittest.TestCase):
    def setUp(self):
        settings=patch.multiple(auth,SUPABASE="https://exampleproject.supabase.co",API="https://voice.example.com",ACCOUNT="test-project")
        settings.start(); self.addCleanup(settings.stop)
    def tearDown(self): auth.FLOW=None

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


if __name__=='__main__': unittest.main()
