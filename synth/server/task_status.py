"""Project acoustic receipts onto the current cloud document job.

Never rerun a cached old canonical transcript to retry a newer document.
The SQLite capture remains an immutable provenance record for cloud recovery.
"""
from synth.worker.desktop_queue import QueueError


def current_job(api, meeting_id):
    jobs = api.call('GET', '/v1/meetings/' + meeting_id + '/jobs')['items']
    return next((job for job in jobs if job.get('is_current')), None)


def project_task(view, api):
    view = dict(view)
    view['capture_state'] = view['state']
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


def document_action(api, task, action):
    upload = task['checkpoints'].get('uploaded')
    if not upload:
        return False
    job = current_job(api, upload['meeting_id'])
    if job is None:
        raise QueueError('current_document_job_missing')
    api.call('POST', '/v1/jobs/' + job['id'] + '/' + action)
    return True
