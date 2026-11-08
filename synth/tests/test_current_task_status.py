import unittest
from synth.server.task_status import project_task, project_tasks, document_action
import threading


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

    def test_pending_voice_retry_resumes_local_tail_instead_of_retrying_cloud(self):
        task = {'checkpoints': {'uploaded': {'meeting_id': 'meeting'}, 'voice_plan': {'path': 'private'}}}
        self.assertFalse(document_action(self, task, 'retry'))
        self.assertEqual(self.calls, [])
        view = project_task({'state': 'failed', 'stage': 'confirming_voices', 'meeting_id': 'meeting',
                             'voice_confirmation_pending': True, 'error_code': 'api_unavailable'}, self)
        self.assertEqual(view['state'], 'failed')
        self.assertEqual(view['error_code'], 'api_unavailable')
        self.assertTrue(view['can_retry'])
        self.assertEqual(self.calls, [])

    def test_pending_voice_cancel_also_requires_local_cancellation(self):
        task = {'checkpoints': {'uploaded': {'meeting_id': 'meeting'}, 'voice_plan': {'path': 'private'}}}
        self.assertFalse(document_action(self, task, 'cancel'))
        self.assertIn(('POST', '/v1/jobs/new/cancel'), self.calls)

    def test_applied_voice_retry_still_uses_current_document_only(self):
        task = {'checkpoints': {'uploaded': {'meeting_id': 'meeting'}, 'voice_plan': {}, 'voice_applied': {}}}
        self.assertTrue(document_action(self, task, 'retry'))
        self.assertIn(('POST', '/v1/jobs/new/retry'), self.calls)

    def test_backend_unavailable_does_not_claim_old_document_ready(self):
        class Offline:
            def call(self, *args):
                raise ValueError('api_unavailable')
        view = project_task({'state': 'succeeded', 'meeting_id': 'meeting'}, Offline())
        self.assertEqual(view['state'], 'unavailable')
        self.assertFalse(view['can_retry'])

    def test_parallel_projection_is_bounded_and_preserves_row_order(self):
        barrier = threading.Barrier(4)
        lock = threading.Lock()
        active = peak = 0
        class API:
            def call(self, method, path):
                nonlocal active, peak
                with lock:
                    active += 1; peak = max(peak, active)
                barrier.wait(timeout=3)
                meeting = path.split('/')[-2]
                with lock: active -= 1
                return {'items': [{'id': 'job-'+meeting, 'state': 'running', 'is_current': True,
                                   'transcript_version': 2, 'attempts': 1}]}
        views = [{'id': str(i), 'state': 'succeeded', 'meeting_id': str(i)} for i in range(8)]
        result = project_tasks(views, API())
        self.assertEqual(peak, 4)
        self.assertEqual([v['job_id'] for v in result], ['job-'+str(i) for i in range(8)])
        self.assertTrue(all(v['state']=='running' and v['capture_state']=='succeeded' for v in result))
        self.assertTrue(all(v['state']=='succeeded' for v in views))

    def test_backend_outage_does_not_wait_once_per_historical_capture(self):
        calls = []
        class API:
            def call(self, *args):
                calls.append(args)
                raise ValueError('api_unavailable')
        views = [{'id': str(i), 'state': 'succeeded', 'meeting_id': str(i)} for i in range(20)]
        views.append({'id': 'local', 'state': 'queued', 'meeting_id': None})
        result = project_tasks(views, API())
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(v['state']=='unavailable' and not v['can_retry'] for v in result[:-1]))
        self.assertEqual(result[-1]['state'], 'queued')
        self.assertEqual(result[-1]['capture_state'], 'queued')

    def test_missing_meetings_do_not_hide_later_available_documents(self):
        class API:
            def call(self, method, path):
                index = int(path.split('/')[-2])
                if index < 4: raise ValueError('api_http_404')
                return {'items': [{'id': str(index), 'state': 'succeeded', 'is_current': True,
                                   'transcript_version': 1, 'attempts': 1}]}
        views = [{'state': 'succeeded', 'meeting_id': str(i)} for i in range(8)]
        result = project_tasks(views, API())
        self.assertTrue(all(v['state']=='succeeded' for v in result[4:]))
