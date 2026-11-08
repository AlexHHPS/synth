"""Canonical provenance for private acoustic observations; no crypto dependency."""
import json
import re
from uuid import UUID

RATE = 16000
POLICY = 'guarded_disjoint_voice_windows_v1'


def binding(capture_id, owner, source, model, model_lock_sha256, crop_seconds, ranges, input_sha256):
    try:
        capture_id = str(UUID(capture_id))
        if not owner.startswith('keys:'):
            owner = str(UUID(owner))
        elif re.fullmatch(r'keys:[0-9a-f]{64}', owner) is None:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('voice_observation_owner_invalid') from None
    if (source not in ('microphone', 'system') or type(crop_seconds) is not int or crop_seconds not in (5, 10)
            or not isinstance(model, str) or re.fullmatch(r'[0-9a-f]{40}(#crop10s)?', model) is None
            or (crop_seconds == 10) != model.endswith('#crop10s')
            or any(not isinstance(s, str) or re.fullmatch(r'[0-9a-f]{64}', s) is None
                   for s in (input_sha256, model_lock_sha256))):
        raise ValueError('voice_observation_policy_invalid')
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= 128:
        raise ValueError('voice_observation_ranges_invalid')
    previous, frames = -1, 0
    for span in ranges:
        if (not isinstance(span, (list, tuple)) or len(span) != 2
                or any(type(x) is not int for x in span) or not 0 <= span[0] < span[1] <= 86400*RATE
                or span[0] < previous):
            raise ValueError('voice_observation_ranges_invalid')
        previous = span[1]; frames += span[1]-span[0]
    if frames != crop_seconds*RATE:
        raise ValueError('voice_observation_ranges_invalid')
    return json.dumps({'version': 1, 'capture_id': capture_id, 'owner': owner, 'source': source,
        'model': model, 'model_lock_sha256': model_lock_sha256, 'crop_seconds': crop_seconds,
        'ranges': ranges, 'input_sha256': input_sha256, 'sample_rate': RATE,
        'clock': 'recording_elapsed', 'policy': POLICY}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def parse(text):
    try:
        value = json.loads(text)
        if not isinstance(value, dict) or set(value) != {
                'version', 'capture_id', 'owner', 'source', 'model', 'model_lock_sha256', 'crop_seconds',
                'ranges', 'input_sha256', 'sample_rate', 'clock', 'policy'}:
            raise ValueError()
        expected = binding(**{k: value[k] for k in (
            'capture_id', 'owner', 'source', 'model', 'model_lock_sha256', 'crop_seconds', 'ranges', 'input_sha256')})
        if expected != text:
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError):
        raise ValueError('voice_observation_binding_invalid') from None


def overlap(left, right):
    """Same clock across channels: an echo cannot cast another independent vote."""
    return any(max(a, c) < min(b, d) for a, b in left for c, d in right)
