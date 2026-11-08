"""Qwen3-ASR adapter with pinned local assets and offline forced alignment."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import wave

from synth.contracts.transcript import Transcript
from .journal import file_hash
from synth.worker.subprocesses import run_process

CONFIG = Path(os.environ.get('SYNTH_ASR_CONFIG', str(Path.home() / 'Library/Application Support/dev.synth.voice/asr-config.json')))
LOCK = Path(__file__).with_name('qwen-model-lock.json')
RUNNER = Path(__file__).with_name('qwen_runner.py')
_VERIFIED = set()


def verify_runtime(configuration):
    if not isinstance(configuration, dict) or not all(isinstance(configuration.get(k), str) and configuration[k]
                                                     for k in ('python', 'asr', 'aligner')):
        raise ValueError('qwen_runtime_invalid')
    lock = json.loads(LOCK.read_text())
    python = Path(configuration['python'])
    if not python.is_file() or not RUNNER.is_file():
        raise ValueError('qwen_runtime_missing')
    for role, model in lock['models'].items():
        root = Path(configuration[role])
        for item in model['files']:
            path = root/item['path']
            if not path.is_file():
                raise ValueError('qwen_model_missing')
            stat = path.stat()
            signature = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, item['sha256'])
            if signature not in _VERIFIED:
                if stat.st_size != item['size'] or file_hash(path) != item['sha256']:
                    raise ValueError('qwen_model_hash_mismatch')
                _VERIFIED.add(signature)
    digest = hashlib.sha256(LOCK.read_bytes() + RUNNER.read_bytes()
                            + RUNNER.with_name('voice_activity.py').read_bytes()).hexdigest()
    return 'qwen3-asr-1.7b-bf16+aligner:' + digest


def transcribe(wav, working_directory, source_id='import', lock_fd=None, configuration=None, prompt=None):
    configuration = configuration or json.loads(CONFIG.read_text())
    fingerprint = verify_runtime(configuration)
    if prompt:
        if not isinstance(prompt, str) or len(prompt)>4000:
            raise ValueError('qwen_prompt_invalid')
        import hashlib
        fingerprint += ':vocabulary-'+hashlib.sha256(prompt.encode()).hexdigest()
    with wave.open(str(wav)) as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
            raise ValueError('asr_input_must_be_mono_16khz_pcm16')
        duration_ms = round(audio.getnframes()/16)
    directory = Path(working_directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    audio_sha256 = file_hash(wav)
    request = {'audio': str(Path(wav).resolve()), 'model': configuration['asr'], 'aligner': configuration['aligner'],
               'decoded_cache': str((directory/'decoded-asr.json').resolve()),
               'cache_key': audio_sha256 + ':' + fingerprint}
    if prompt:
        request['system_prompt'] = prompt
    env = {**os.environ, 'HF_HUB_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1', 'SSL_CERT_FILE': '/etc/ssl/cert.pem'}
    try:
        result = run_process([configuration['python'], '-I', str(RUNNER)],
                                input=json.dumps(request), text=True, capture_output=True,
                                env=env, timeout=min(14400, duration_ms/1000*3+120),
                                pass_fds=() if lock_fd is None else (lock_fd,))
    except subprocess.TimeoutExpired:
        raise ValueError('qwen_asr_timeout') from None
    if result.returncode:
        failure = 'qwen_asr_failed'
        try:
            error = json.loads(result.stdout)
            code = error.get('error_code', '')
            if isinstance(code, str) and code.startswith(('qwen_', 'asr_')) and code.replace('_', '').isalnum():
                (directory/'qwen-error.json').write_text(json.dumps({'error_code': code})+'\n')
                if code in {'qwen_alignment_invalid_time', 'qwen_alignment_nonmonotonic',
                            'qwen_alignment_incomplete', 'qwen_alignment_text_mismatch',
                            'qwen_token_limit_reached', 'qwen_asr_incomplete'}:
                    failure = code
        except (ValueError, AttributeError):
            pass
        raise ValueError(failure)
    raw = json.loads(result.stdout)
    segments = [{**segment, 'id': f's{i:06d}', 'source_id': source_id}
                for i, segment in enumerate(raw['segments'])]
    transcript = {'language': 'es', 'duration_ms': duration_ms,
                  'model_fingerprint': fingerprint, 'segments': segments}
    # An empty source is valid internally; the merged canonical transcript must
    # still satisfy Transcript's non-empty constraint before publication.
    if segments:
        transcript = Transcript.model_validate(transcript).model_dump()
    raw.update(mode='real', audio_sha256=audio_sha256, model_fingerprint=fingerprint,
               sample_rate=16000, segments=segments)
    directory = Path(working_directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (directory/'raw-asr.json').write_text(json.dumps(raw, ensure_ascii=False, indent=2)+'\n')
    (directory/'transcript.json').write_text(json.dumps(transcript, ensure_ascii=False, indent=2)+'\n')
    (directory/'qwen-error.json').unlink(missing_ok=True)
    return transcript, raw
