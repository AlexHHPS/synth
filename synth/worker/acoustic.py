"""Prepare source-consistent audio for ASR, diarization and voice matching."""
import json
import os
from pathlib import Path
import subprocess
import wave

from .journal import file_hash
from synth.worker.subprocesses import run_process
from .qwen_asr import CONFIG

RUNNER = Path(__file__).with_name('echo.py')


def prepare_acoustic_sources(normalized, raw, lock_fd=None, configuration=None):
    configuration = configuration if configuration is not None else (json.loads(CONFIG.read_text()) if CONFIG.is_file() else {})
    result = {'sources': dict(normalized), 'echo': {'skipped': 'not_enabled'}}
    if configuration.get('echo_cancellation') != 'aec3':
        return result
    if not {'microphone', 'system'} <= set(normalized):
        result['echo'] = {'skipped': 'single_source'}
        return result
    python = configuration.get('python')
    if not python or not Path(python).is_file() or not RUNNER.is_file():
        raise ValueError('echo_runtime_missing')
    target = Path(raw)/'microphone.aec.wav'
    temporary = target.with_name('microphone.aec.part.wav')
    request = {'microphone': normalized['microphone']['path'], 'system': normalized['system']['path'], 'output': str(temporary)}
    with wave.open(request['microphone']) as mic:
        frames = mic.getnframes()
        duration = frames/mic.getframerate()
    try:
        process = run_process([python, '-I', str(RUNNER)],
                                 input=json.dumps(request), text=True, capture_output=True,
                                 env={**os.environ, 'HF_HUB_OFFLINE': '1'}, timeout=min(3600, duration+60),
                                 pass_fds=() if lock_fd is None else (lock_fd,))
        if process.returncode or not temporary.is_file():
            raise ValueError('echo_processing_failed')
        stats = json.loads(process.stdout)
        with wave.open(str(temporary)) as output:
            if (output.getframerate(), output.getnchannels(), output.getsampwidth(), output.getnframes()) != (16000, 1, 2, frames):
                raise ValueError('echo_output_invalid')
        temporary.chmod(0o600)
        temporary.replace(target)
    except subprocess.TimeoutExpired:
        raise ValueError('echo_processing_timeout') from None
    finally:
        temporary.unlink(missing_ok=True)
    result['sources']['microphone'] = {'path': str(target), 'sha256': file_hash(target)}
    result['echo'] = stats
    return result
