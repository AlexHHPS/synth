import unittest
from synth.server.task_status import project_task, document_action


class CurrentTaskTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.jobs = [{'id': 'old', 'state': 'succeeded', 'is_current': False, 'transcript_version': 1, 'attempts': 3},
                     {'id': 'new', 'state': 'failed', 'is_current': True, 'transcript_version': 2, 'attempts': 2,
                      'error_code': 'document_invalid_citation'}]

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        return {'items': self.jobs}

    def test_current_cloud_failure_overrides_old_local_success(self):
        local = {'state': 'succeeded', 'meeting_id': 'meeting', 'job_id': 'old'}
        view = project_task(local, self)
        self.assertEqual(view['state'], 'failed')
        self.assertEqual(view['capture_state'], 'succeeded')
        self.assertEqual(view['transcript_version'], 2)
        self.assertEqual(view['job_id'], 'new')
        self.assertEqual(local['job_id'], 'old')

    def test_retry_routes_to_current_job_without_retranscribing(self):
        task = {'checkpoints': {'uploaded': {'meeting_id': 'meeting', 'job_id': 'old'}}}
        self.assertTrue(document_action(self, task, 'retry'))
        self.assertIn(('POST', '/v1/jobs/new/retry'), self.calls)
        self.assertNotIn(('POST', '/v1/jobs/old/retry'), self.calls)

    def test_backend_unavailable_does_not_claim_old_document_ready(self):
        class Offline:
            def call(self, *args):
                raise ValueError('api_unavailable')
        view = project_task({'state': 'succeeded', 'meeting_id': 'meeting'}, Offline())
        self.assertEqual(view['state'], 'unavailable')
        self.assertFalse(view['can_retry'])
