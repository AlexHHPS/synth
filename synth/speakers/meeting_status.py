"""Read automatic voice results only for the matching transcript and catalog."""
import hashlib
import json
from pathlib import Path


def transcript_hash(content):
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def audio_available(task):
    descriptors = task['checkpoints'].get('normalized', {})
    return bool(descriptors) and all(Path(d['path']).is_file() for d in descriptors.values())


def meeting_status(task, transcript, output, signature):
    base = {'state': 'unavailable', 'matches': {}, 'processing_mode': 'automatic',
            'transcript_version': transcript['version'], 'task_id': task['id']}
    canonical_path = Path(output) / 'transcript.json'
    if not canonical_path.is_file():
        return {**base, 'reason': 'analysis_pending' if task['state'] in ('queued', 'running') else 'native_audio_unavailable'}
    canonical = json.loads(canonical_path.read_text())
    if canonical != transcript['content']:
        return {**base, 'reason': 'transcript_version_changed'}
    path = Path(output) / 'identity-status.json'
    if not path.is_file():
        return {**base, 'reason': 'analysis_pending' if audio_available(task) else 'audio_deleted'}
    result = json.loads(path.read_text())
    if result.get('transcript_hash') not in (None, transcript_hash(canonical)):
        return {**base, 'reason': 'transcript_version_changed'}
    if result.get('profile_signature') != signature:
        return {**base, 'reason': 'profile_changed' if audio_available(task) else 'audio_deleted'}
    speakers = {s.get('speaker_id') for s in canonical['segments'] if s.get('speaker_id')}
    # Mixed live recovery has no trustworthy mapping to source-track speakers.
    if not speakers:
        return {**base, 'reason': 'transcript_has_no_speaker_mapping'}
    return {**result, **{k: base[k] for k in ('processing_mode', 'transcript_version', 'task_id')},
            'matches': {k: v for k, v in result.get('matches', {}).items() if k in speakers}}
