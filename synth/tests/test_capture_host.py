import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from synth.server import desktop_auth as auth, desktop_host as host


class CaptureHostTests(unittest.TestCase):
    def test_google_callback_has_safe_html_and_specific_configuration_diagnosis(self):
        with patch.object(auth,'FLOW',{'calendar':True}), patch.object(auth,'callback',side_effect=ValueError('calendar_client_configuration_invalid')):
            server=host.ThreadingHTTPServer(('127.0.0.1',0),host.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                opener=build_opener(ProxyHandler({}))
                with self.assertRaises(HTTPError) as caught:
                    opener.open(f'http://127.0.0.1:{server.server_port}/auth/callback/synthetic?code=secret-one-use-code',timeout=3)
                response=caught.exception
                self.assertIn('text/html',response.headers['Content-Type'])
                self.assertEqual(response.headers['Cache-Control'],'no-store')
                self.assertEqual(response.headers['Referrer-Policy'],'no-referrer')
                body=response.read().decode()
                self.assertIn('Tu sesión de Synth sigue abierta',body)
                self.assertIn('secreto original',body)
                self.assertNotIn('secret-one-use-code',body)
                self.assertNotIn('https://',body)
            finally:
                server.shutdown();server.server_close();thread.join(timeout=3)

    def test_slow_context_cannot_replace_a_newer_note_and_offline_start_clears_old_terms(self):
        from uuid import uuid4
        owner, old_folder = str(uuid4()), str(uuid4())
        old_started, release_old = threading.Event(), threading.Event()
        api = Mock(auth_mode='supabase')
        api.assert_current_principal = Mock()
        def call(method, path, body=None):
            if path == '/v1/me': return {'kind':'user','id':owner}
            if old_folder in path:
                old_started.set()
                if not release_old.wait(3): raise ValueError('test_timeout')
                return {'items':[{'term':'Old confidential client'}]}
            return {'items':[{'term':'Latest context'}]}
        api.call.side_effect = call
        with tempfile.TemporaryDirectory() as directory, patch.object(host,'APPDATA',Path(directory)), \
                patch.object(host,'STATE',Path(directory)), patch.object(host,'LocalAPI',return_value=api), \
                patch.object(auth,'capture_identity',return_value={'mode':'supabase','signed_in':True,'principal_id':owner}):
            (Path(directory)/'desktop-api-key').write_text('synthetic-local-only-key')
            server=host.ThreadingHTTPServer(('127.0.0.1',0),host.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            url=f'http://127.0.0.1:{server.server_port}/v1/capture/context'
            opener=build_opener(ProxyHandler({}))
            def send(folder):
                request=Request(url,data=json.dumps({'folder_id':folder}).encode(),headers={'Authorization':'Bearer synthetic-local-only-key'})
                with opener.open(request,timeout=5) as response:return json.load(response)
            try:
                with self.assertRaises(HTTPError):opener.open(Request(url,data=b'{}'),timeout=3)
                self.assertFalse((Path(directory)/'prepared-capture-context.json').exists())
                results=[]
                first=threading.Thread(target=lambda:results.append(send(old_folder)));first.start()
                self.assertTrue(old_started.wait(2))
                self.assertTrue(send(None)['prepared'])
                release_old.set();first.join(timeout=3)
                self.assertEqual(results,[{'prepared':False,'terms':0}])
                cached=json.loads((Path(directory)/'prepared-capture-context.json').read_text())
                self.assertEqual(cached['terms'],['Latest context'])
                api.call.side_effect=ValueError('backend_unavailable')
                with self.assertRaises(HTTPError):send(None)
                self.assertEqual(json.loads((Path(directory)/'prepared-capture-context.json').read_text())['terms'],[])
            finally:
                release_old.set();server.shutdown();server.server_close();thread.join(timeout=3)

    def test_live_voice_route_is_authenticated_rechecks_principal_and_preserves_owner_denial(self):
        from uuid import uuid4
        from synth.speakers import live_status
        capture, owner = str(uuid4()), str(uuid4())
        api = Mock(auth_mode='supabase')
        api.assert_current_principal = Mock()
        api.call.return_value = {'kind': 'user', 'id': owner}
        with tempfile.TemporaryDirectory() as directory, patch.object(host, 'APPDATA', Path(directory)), \
                patch.object(host, 'LocalAPI', return_value=api), \
                patch.object(live_status, 'capture_status', return_value={'capture_id': capture, 'state': 'collecting', 'matches': []}) as status, \
                patch.object(live_status, 'confirm_capture', return_value={'capture_id': capture, 'confirmation_revision': 1}) as confirm:
            (Path(directory)/'desktop-api-key').write_text('synthetic-local-only-key')
            server = host.ThreadingHTTPServer(('127.0.0.1', 0), host.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}/v1/voice/captures/{capture}'
                opener = build_opener(ProxyHandler({}))
                with self.assertRaises(HTTPError) as error:
                    opener.open(url, timeout=3)
                self.assertEqual(error.exception.code, 401)
                status.assert_not_called()
                with self.assertRaises(HTTPError) as error:
                    opener.open(Request(url+'/speakers', data=b'{}'), timeout=3)
                self.assertEqual(error.exception.code, 401)
                confirm.assert_not_called()
                request = Request(url, headers={'Authorization': 'Bearer synthetic-local-only-key'})
                with opener.open(request, timeout=3) as response:
                    self.assertEqual(json.load(response)['capture_id'], capture)
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertEqual(status.call_args.args[:3], (capture, owner, api.call.return_value))
                self.assertGreaterEqual(api.assert_current_principal.call_count, 2)
                decision = {'confirmed_by_user': True}
                post = Request(url+'/speakers', data=json.dumps(decision).encode(),
                               headers={'Authorization': 'Bearer synthetic-local-only-key'})
                with opener.open(post, timeout=3) as response:
                    self.assertEqual(json.load(response)['confirmation_revision'], 1)
                self.assertEqual(confirm.call_args.args[:3], (capture, owner, api.call.return_value))
                self.assertEqual(confirm.call_args.args[-1], decision)
                self.assertIs(confirm.call_args.kwargs['guard'], api.assert_current_principal)
                status.side_effect = ValueError('voice_capture_owner_mismatch')
                with self.assertRaises(HTTPError) as error:
                    opener.open(request, timeout=3)
                self.assertEqual(error.exception.code, 409)
                self.assertEqual(json.load(error.exception)['detail'], 'voice_capture_owner_mismatch')
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_local_identity_route_requires_local_auth_and_never_fetches_cloud_configuration(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(host, 'APPDATA', Path(directory)), \
             patch.object(auth, 'APPDATA', Path(directory)), \
             patch.object(auth, 'fetch', side_effect=AssertionError('Must remain offline')):
            (Path(directory)/'desktop-api-key').write_text('synthetic-local-only-key')
            (Path(directory)/'backend-config.json').write_text(json.dumps({'auth_mode': 'supabase'}))
            with patch.object(auth, 'read_session', return_value={'principal_id': 'user-a', 'access_token': 'secret-not-returned'}):
                server = host.ThreadingHTTPServer(('127.0.0.1', 0), host.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f'http://127.0.0.1:{server.server_port}/v1/auth/capture-identity'
                    opener = build_opener(ProxyHandler({}))
                    with self.assertRaises(HTTPError) as error:
                        opener.open(url, timeout=3)
                    self.assertEqual(error.exception.code, 401)
                    request = Request(url, headers={'Authorization': 'Bearer synthetic-local-only-key'})
                    with opener.open(request, timeout=3) as response:
                        self.assertEqual(response.headers['Cache-Control'], 'no-store')
                        self.assertEqual(json.load(response), {'mode': 'supabase', 'signed_in': True, 'principal_id': 'user-a'})
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
