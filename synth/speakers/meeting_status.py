"""Voice suggestions for the transcript the owner is looking at (never identities)."""
import hashlib
import json
from pathlib import Path
from .windows import analysis_policy


def transcript_hash(content):
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def timeline_hash(content):
    """Same recording and raw words regardless of owner-resolved names or glossary corrections."""
    applied = {}
    for correction in content.get("corrections") or []:
        if correction.get("status") == "applied":
            applied.setdefault(correction["segment_id"], []).append(correction)
    rows = []
    for s in content["segments"]:
        text = s.get("text")
        for correction in applied.get(s.get("id"), []):
            text = text.replace(correction["replacement"], correction["original"], 1)
        rows.append((s.get("id"), s.get("start_ms"), s.get("end_ms"), text))
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def audio_available(task):
    descriptors = task['checkpoints'].get('normalized', {})
    return bool(descriptors) and all(Path(d['path']).is_file() for d in descriptors.values())


def meeting_status(task, transcript, output, signature, reidentify=None):
    base = {'state': 'unavailable', 'matches': {}, 'processing_mode': 'automatic',
            'transcript_version': transcript['version'], 'task_id': task['id']}
    canonical_path = Path(output) / 'transcript.json'
    if not canonical_path.is_file():
        return {**base, 'reason': 'analysis_pending' if task['state'] in ('queued', 'running') else 'native_audio_unavailable'}
    canonical = json.loads(canonical_path.read_text())
    content = transcript['content']
    if timeline_hash(canonical) != timeline_hash(content):
        return {**base, 'reason': 'transcript_version_changed'}
    path = Path(output) / 'identity-status.json'
    if not path.is_file():
        return {**base, 'reason': 'analysis_pending' if audio_available(task) else 'audio_deleted'}
    result = json.loads(path.read_text())
    if result.get('transcript_hash') not in (None, transcript_hash(canonical)):
        return {**base, 'reason': 'transcript_version_changed'}
    if result.get('profile_signature') != signature:
        if reidentify is None:
            return {**base, 'reason': 'profile_changed'}
        result = reidentify()
    legacy_policy = result.get('legacy_analysis_policy', result.get('analysis_policy'))
    legacy_result = result.get('matching_mode') in ('pilot_uncalibrated', 'policy_separated') or any(
        value.get('state') == 'candidate' for value in result.get('matches', {}).values())
    if legacy_result and legacy_policy != analysis_policy():
        return {**base, 'reason': 'analysis_policy_changed'}
    labels = {s.get('speaker_id') for s in content['segments'] if s.get('speaker_id')}
    if not labels:
        return {**base, 'reason': 'transcript_has_no_speaker_mapping'}
    resolved = set((content.get('speakers') or {}))
    open_labels = labels - resolved
    return {**result, **{k: base[k] for k in ('processing_mode', 'transcript_version', 'task_id')},
            'matches': {k: v for k, v in result.get('matches', {}).items() if k in open_labels},
            'resolved': sorted(resolved & labels)}
