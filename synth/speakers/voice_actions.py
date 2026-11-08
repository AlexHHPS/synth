"""Host side of an owner's speaker decision: forward it, then erase that speaker's voiceprint."""
from uuid import UUID

ACTIONS = {"confirm", "reject", "rename", "merge"}


def _forget(task_id, speaker_id):
    from .prints import open_prints
    store = open_prints()
    try:
        store.forget(task_id, speaker_id)
    finally:
        store.close()


def resolve_speaker(api, meeting_id, task_id, body):
    if not isinstance(body, dict) or body.get("action") not in ACTIONS or not isinstance(body.get("speaker_id"), str):
        raise ValueError("speaker_action_invalid")
    meeting_id = str(UUID(meeting_id))
    try:
        result = api.call("POST", f"/v1/meetings/{meeting_id}/speakers", body)
    except ValueError as error:
        # A retried decision after a lost response: the cloud already resolved this speaker.
        if str(error) == "api_http_409" and task_id:
            current = api.call("GET", f"/v1/meetings/{meeting_id}/transcript")["content"]
            labels = {s.get("speaker_id") for s in current["segments"]}
            if body["speaker_id"] in (current.get("speakers") or {}) or body["speaker_id"] not in labels:
                _forget(task_id, body["speaker_id"])
        raise
    # The decision is recorded in the cloud transcript; the print served its purpose.
    if task_id:
        _forget(task_id, body["speaker_id"])
    return result
