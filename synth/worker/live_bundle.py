"""Source files needed by the isolated Python, outside PyInstaller's importer."""
import hashlib
import json
from pathlib import Path

PROTOCOL_VERSION = 3
FILES = (
    'live_bundle.py', 'qwen_live_runner.py', 'qwen_runner.py', 'qwen-model-lock.json',
    'qwen-runtime.requirements.txt', 'incremental_audio.py', 'echo.py', 'voice_activity.py',
    'consolidation.py', 'contextual_audio.py', 'consolidated_capture.py', 'word_seams.py', 'speaker_session.py', 'capture_vocabulary.py',
    'voice_binding.py', 'voice_key.py', 'voice_windows.py', 'live_voice.py',
)
MANIFEST = 'live-runtime.json'


def describe(directory):
    directory = Path(directory)
    hashes = {}
    for name in FILES:
        with (directory/name).open('rb') as stream:
            hashes[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'version': 1, 'protocol_version': PROTOCOL_VERSION, 'sha256': hashes}


def verify(directory):
    """Reject missing/changed isolated dependencies even if the frozen host imports."""
    directory = Path(directory)
    try:
        expected = json.loads((directory/MANIFEST).read_text())
        actual = describe(directory)
    except (OSError, ValueError):
        raise ValueError('qwen_live_bundle_missing') from None
    if expected != actual:
        raise ValueError('qwen_live_bundle_changed')
    return actual


def data_arguments(directory, manifest):
    """Explicit public code allowlist; no user models, profiles or recordings."""
    directory = Path(directory)
    arguments = []
    for path in [*(directory/name for name in FILES), Path(manifest)]:
        arguments.extend(['--add-data', str(path)+':synth/worker'])
    return arguments
