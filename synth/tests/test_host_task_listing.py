from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from synth.server import desktop_host as host
from synth.worker.desktop_queue import DesktopQueue


class TaskListingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.capture = self.root/'capture';self.capture.mkdir()
        self.database = self.root/'queue.sqlite'
        self.owner = str(uuid4())
        audio = self.capture/'sample.wav';audio.write_bytes(b'public synthetic audio')
        queue = DesktopQueue(self.database, self.capture)
        task = queue.enqueue({'import': str(audio)}, 'Private owner title', consent_confirmed=True,
                             owner_id=self.owner)
        self.task_id = task['id']
        queue.close()
        (self.root/'desktop-api-key').write_text('synthetic-test-key')

    def request(self, switch_account=False, path='/v1/tasks', method='GET', switch_after=0):
        owner = self.owner
        class API:
            checks = 0
            def call(self, method, path):
                if path == '/v1/me':return {'id': owner, 'kind': 'user', 'name': 'Synthetic owner'}
                raise AssertionError(path)
            def assert_current_principal(self):
                self.checks += 1
                if switch_account and self.checks > switch_after:raise ValueError('auth_identity_changed')
        with patch.object(host, 'APPDATA', self.root), patch.object(host, 'DATABASE', self.database), \
             patch.object(host, 'CAPTURE', self.capture), patch.object(host, 'LocalAPI', return_value=API()):
            server = ThreadingHTTPServer(('127.0.0.1', 0), host.Handler)
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
            thread.start()
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                connection.request(method, path, headers={'Authorization': 'Bearer synthetic-test-key'})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close();server.shutdown();server.server_close();thread.join(timeout=2)

    def test_authenticated_task_listing_still_returns_owned_capture(self):
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body['items'][0]['title'], 'Private owner title')
        self.assertEqual(body['items'][0]['state'], 'queued')

    def test_account_switch_discards_response_instead_of_returning_old_titles(self):
        status, body = self.request(switch_account=True)
        self.assertEqual(status, 409)
        self.assertEqual(body['detail'], 'auth_identity_changed')
        self.assertNotIn('Private owner title', json.dumps(body))

    def test_account_switch_discards_single_task_response(self):
        status, body = self.request(switch_account=True, path='/v1/tasks/' + self.task_id)
        self.assertEqual((status, body['detail']), (409, 'auth_identity_changed'))
        self.assertNotIn('Private owner title', json.dumps(body))

    def test_account_switch_cannot_cancel_previous_accounts_local_capture(self):
        status, body = self.request(switch_account=True, path='/v1/tasks/' + self.task_id + '/cancel', method='POST')
        self.assertEqual((status, body['detail']), (409, 'auth_identity_changed'))
        queue = DesktopQueue(self.database, self.capture)
        try:
            self.assertEqual(queue.get(self.task_id)['state'], 'queued')
        finally:
            queue.close()

    def test_account_switch_discards_voice_profile_response(self):
        with patch.object(host.onboarding, 'status', return_value={'employee_name': 'Private owner voice profile'}):
            status, body = self.request(switch_account=True, path='/v1/voice', switch_after=1)
        self.assertEqual((status, body['detail']), (409, 'auth_identity_changed'))
        self.assertNotIn('Private owner voice profile', json.dumps(body))

    def test_capture_is_rejected_before_enqueue_while_qwen_is_preparing(self):
        with patch.object(host.audio_setup,'ready',return_value=False):
            status,body=self.request(path='/v1/captures',method='POST')
        self.assertEqual((status,body['detail']),(409,'audio_setup_not_ready'))
