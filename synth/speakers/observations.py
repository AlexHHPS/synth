"""Host-only reader of authenticated local voice observations, never an API DTO."""
import base64
import hashlib
import json
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from synth.worker.voice_binding import parse
from .native import REVISION
from .profiles import normalized


def load_observation(descriptor, expected_binding, directory, key, model_lock=None):
    expected = parse(expected_binding)
    from .native import LOCK
    lock_path = Path(model_lock or LOCK)
    lock_content = lock_path.read_bytes()
    if (hashlib.sha256(lock_content).hexdigest() != expected['model_lock_sha256']
            or json.loads(lock_content)['model_revision'] != expected['model'].split('#')[0]):
        raise ValueError('voice_observation_model_mismatch')
    root = Path(directory).resolve(strict=True)
    path = Path(descriptor['path']).absolute()
    if path != path.resolve(strict=True) or not path.is_relative_to(root):
        raise ValueError('voice_observation_path_denied')
    if path.stat().st_size > 256*1024:
        raise ValueError('voice_observation_too_large')
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != descriptor['sha256']:
        raise ValueError('voice_observation_file_changed')
    try:
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError()
        envelope = json.loads(content)
        if (set(envelope) != {'version', 'binding', 'combined'} or envelope['version'] != 1
                or envelope['binding'] != expected_binding):
            raise ValueError()
        combined = base64.b64decode(envelope['combined'], validate=True)
        value = json.loads(AESGCM(key).decrypt(combined[:12], combined[12:], expected_binding.encode()))
    except (ValueError, KeyError, TypeError, InvalidTag):
        raise ValueError('voice_observation_authentication_failed') from None
    if (not isinstance(value, dict) or set(value) != {'embedding', 'rho128', 'duration_ms', 'revision'}
            or value['revision'] != REVISION or type(value['duration_ms']) is not int
            or value['duration_ms'] != expected['crop_seconds']*1000):
        raise ValueError('voice_observation_payload_invalid')
    for name, size in (('embedding', 256), ('rho128', 128)):
        if not isinstance(value[name], list):
            raise ValueError('voice_observation_payload_invalid')
        try:
            unit = normalized(value[name], size)
        except ValueError:
            raise ValueError('voice_observation_payload_invalid') from None
        if max(abs(a-b) for a, b in zip(unit, value[name])) > .00001:
            raise ValueError('voice_observation_payload_invalid')
    return {**expected, 'embedding': value['embedding'], 'rho128': value['rho128']}
