"""Project acoustic receipts onto the current cloud document job.

Never rerun a cached old canonical transcript to retry a newer document.
The SQLite capture remains an immutable provenance record for cloud recovery.
"""
from synth.worker.desktop_queue import QueueError
from concurrent.futures import ThreadPoolExecutor
import threading


def pending_voice_confirmation(task):
    checkpoints = task.get('checkpoints', {})
    return ('uploaded' in checkpoints and 'voice_plan' in checkpoints
            and 'voice_applied' not in checkpoints)


def current_job(api, meeting_id):
    jobs = api.call('GET', '/v1/meetings/' + meeting_id + '/jobs')['items']
    return next((job for job in jobs if job.get('is_current')), None)


def project_task(view, api):
    view = dict(view)
    view['capture_state'] = view['state']
    if view.get('voice_confirmation_pending'):
        return {**view, 'can_retry': view['state'] == 'failed'}
    if not view.get('meeting_id'):
        return view
    try:
        job = current_job(api, view['meeting_id'])
    except ValueError:
        return {**view, 'state': 'unavailable', 'error_code': 'api_unavailable', 'can_retry': False}
    if job is None:
        return {**view, 'state': 'unavailable', 'error_code': 'current_document_job_missing', 'can_retry': False}
    return {**view, 'state': job['state'], 'stage': 'uploaded', 'job_id': job['id'],
            'transcript_version': job['transcript_version'], 'attempts': job['attempts'],
            'error_code': job.get('error_code'), 'can_retry': job['state'] == 'failed'}


def project_tasks(views, api, max_workers=4):
    """Bounded concurrent reads, ordered results, no cross-user status cache."""
    outage = threading.Event()

    class RequestAPI:
        def call(self, *args, **kwargs):
            try:
                return api.call(*args, **kwargs)
            except ValueError as error:
                if str(error) in {'api_unavailable', 'api_http_429', 'api_http_502', 'api_http_503', 'api_http_504'}:
                    outage.set()
                raise

    projected = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for start in range(0, len(views), max_workers):
            if outage.is_set():
                projected.extend({**view, 'capture_state': view['state'], 'state': 'unavailable',
                                  'error_code': 'api_unavailable', 'can_retry': False} if view.get('meeting_id')
                                 else {**view, 'capture_state': view['state']} for view in views[start:])
                break
            projected.extend(executor.map(lambda view: project_task(view, RequestAPI()), views[start:start+max_workers]))
    return projected


def document_action(api, task, action):
    upload = task['checkpoints'].get('uploaded')
    if not upload:
        return False
    pending = pending_voice_confirmation(task)
    if pending and action == 'retry':
        # The durable local plan must be applied before retrying any document.
        # Resuming canonical/uploaded checkpoints never needs the deleted audio.
        return False
    job = current_job(api, upload['meeting_id'])
    if job is None:
        if pending and action == 'cancel':
            return False
        raise QueueError('current_document_job_missing')
    api.call('POST', '/v1/jobs/' + job['id'] + '/' + action)
    return not pending  # Cancellation must also stop the pending local tail.
