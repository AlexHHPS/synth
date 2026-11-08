"""Resident, offline Qwen ASR for ordered live audio windows over JSON lines.

No microphone access, HTTP listener or reference text. Contextual capture lazily
loads the pinned local aligner and saves prepared tracks beside the raw capture.
The parent owns the pipes; EOF releases the model and acoustic lease.
"""
import contextlib
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import time

MAX_SAMPLES = 30 * 16000
MAX_REQUEST_BYTES = 12 * 1024 * 1024


def validate_samples(value):
    if not isinstance(value, list) or not 1600 <= len(value) <= MAX_SAMPLES:
        raise ValueError('qwen_live_audio_invalid')
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or abs(x) > 1.01 for x in value):
        raise ValueError('qwen_live_audio_invalid')
    return value


def read_request():
    line = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    if not line:
        return None
    if len(line) > MAX_REQUEST_BYTES or not line.endswith(b'\n'):
        raise ValueError('qwen_live_request_invalid')
    request = json.loads(line)
    if not isinstance(request, dict):
        raise ValueError('qwen_live_request_invalid')
    return request


def send(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def validate_model(directory, specification):
    for item in specification['files']:
        path = directory/item['path']
        if not path.is_file() or path.stat().st_size != item['size']:
            raise ValueError('qwen_model_missing')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != item['sha256']:
                raise ValueError('qwen_model_hash_mismatch')


def main():
    request = read_request()
    if not request or request.get('command') != 'load':
        raise ValueError('qwen_live_request_invalid')
    state = Path(request['state'])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lease = os.open(state/'acoustic-worker.lock', os.O_CREAT | os.O_RDWR, 0o600)
    incremental = journal = prepared = aligner = voice = None
    speaker_configuration = request.get('speakers')
    contextual = published = False
    vocabulary_prompt = None
    try:
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('acoustic_worker_already_running') from None
                time.sleep(.02)
        started = time.monotonic()
        with contextlib.redirect_stdout(sys.stderr):
            lock = json.loads(Path(__file__).with_name('qwen-model-lock.json').read_text())
            for name, version in lock['runtime'].items():
                if importlib.metadata.version(name) != version:
                    raise ValueError('qwen_runtime_version_mismatch')
            directory = Path(request['model'])
            aligner_directory = Path(request['aligner']) if request.get('aligner') else None
            validate_model(directory, lock['models']['asr'])
            import mlx.core as mx
            import numpy as np
            from mlx_audio.stt.utils import load_model
            mx.set_memory_limit(10 * 1024**3)
            mx.set_cache_limit(256 * 1024**2)
            model = load_model(str(directory))
        send({'ready': True, 'protocol_version': 3, 'model': 'Qwen3-ASR-1.7B', 'load_seconds': time.monotonic()-started})
        decoded_blocks = 0
        presence_metrics = {}
        def recognize(source, audio, start, end):
            nonlocal decoded_blocks
            began = time.monotonic()
            text, generated, called = '', 0, False
            with contextlib.redirect_stdout(sys.stderr):
                # ContextualProcessor gates on the entire core; the last 3 s
                # preprocessing slab alone is not evidence of silence for it.
                presence = None if contextual else presence_metrics.get(source)
                if np.any(audio) and (presence is None or presence['has_speech'] is not False):
                    limit = max(128, math.ceil(len(audio)/16000*16))
                    called = True
                    result = model.generate(np.clip(audio, -1, 1), language='Spanish', max_tokens=limit,
                                            temperature=0.0, prefill_step_size=512, verbose=False,
                                            **({'system_prompt': vocabulary_prompt} if vocabulary_prompt else {}))
                    decoded_blocks += 1
                    if result.generation_tokens >= limit:
                        raise ValueError('qwen_live_token_limit')
                    text, generated = result.text.strip(), result.generation_tokens
                mx.clear_cache()
            return {'text': text, 'source': source, 'start_seconds': start, 'end_seconds': end,
                    'state': ('no_speech_detected' if presence['has_speech'] is False else 'provisional_low_voice_evidence'
                              if presence['has_speech'] is None else 'provisional') if presence is not None else 'provisional',
                    'speech_presence': presence, 'model_called': called,
                    'model': 'Qwen3-ASR-1.7B', 'generation_tokens': generated,
                    'elapsed_seconds': time.monotonic()-began}
        while (request := read_request()) is not None:
            if request.get('command') == 'stop':
                break
            if request.get('command') == 'capture':
                if (not {'command', 'folder', 'echo_cancellation'} <= set(request)
                        or set(request)-{'command','folder','echo_cancellation','speech_detection','consolidation'}
                        or request['echo_cancellation'] not in ('off', 'aec3')
                        or request.get('speech_detection', 'off') not in ('off', 'webrtc_rnn')
                        or request.get('consolidation', 'off') not in ('off', 'context3')):
                    raise ValueError('qwen_live_request_invalid')
                # Trusted packaged code, loaded explicitly in the isolated runtime.
                # No model/context/reference text is accepted through this command.
                sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
                from synth.worker.incremental_audio import SourceReader, BlockJournal, IncrementalProcessor, RESAMPLING_POLICY
                from synth.worker.echo import StreamingEchoCanceller
                from synth.worker.voice_activity import SpeechPresence, POLICY as VAD_POLICY
                reader = SourceReader(request['folder'])
                from synth.worker.capture_vocabulary import load as load_vocabulary, prompt as asr_prompt, fingerprint as vocabulary_hash
                terms = load_vocabulary(state, reader.folder, reader.capture_id)
                vocabulary_prompt = asr_prompt(terms)
                progress = reader.progress()
                if incremental is not None:
                    previous = incremental.reader.progress()
                    if previous['state'] != 'closed' or incremental.position < max(previous['sources'].values()):
                        raise ValueError('qwen_live_capture_already_active')
                if journal is not None:
                    journal.close()
                    journal = None
                if prepared is not None:
                    prepared.close()
                    prepared = None
                if voice is not None:
                    voice.close()
                    voice = None
                contextual = request.get('consolidation') == 'context3'
                published = False
                echo_processor = StreamingEchoCanceller() if request['echo_cancellation'] == 'aec3' else None
                speech_detector = SpeechPresence() if request.get('speech_detection') == 'webrtc_rnn' else None
                def prepare(sources):
                    nonlocal presence_metrics
                    processed = echo_processor(sources) if echo_processor else sources
                    # Replayed/cached blocks must also advance VAD and AEC state.
                    presence_metrics = {s: speech_detector.analyze(s, a) for s, a in processed.items()} if speech_detector else {}
                    return (processed, presence_metrics) if contextual else processed
                if contextual:
                    if aligner_directory is None or echo_processor is None or speech_detector is None:
                        raise ValueError('qwen_live_context_config_invalid')
                    from synth.worker.consolidation import ConsolidationJournal
                    from synth.worker.contextual_audio import ContextualProcessor
                    from synth.worker.consolidated_capture import PreparedTracks, inference_policy, publish
                    from synth.worker.qwen_runner import align_chunk
                    with contextlib.redirect_stdout(sys.stderr):
                        if aligner is None:
                            validate_model(aligner_directory, lock['models']['aligner'])
                            aligner = load_model(str(aligner_directory))
                    policy = inference_policy(directory, aligner_directory)
                    policy['vocabulary_sha256'] = vocabulary_hash(terms)
                    journal = ConsolidationJournal(state/'incremental'/reader.capture_id/'consolidation.sqlite3', reader.capture_id, policy)
                    prepared = PreparedTracks(reader.folder, progress['sources'])
                    if speaker_configuration is not None:
                        from synth.worker.live_voice import LiveVoiceObserver
                        try:
                            voice = LiveVoiceObserver(reader.folder, reader.capture_id, state, speaker_configuration, lease)
                        except (OSError, ValueError):
                            # Optional speaker evidence never prevents recording/ASR.
                            voice = None
                    def observe(signals, start, end):
                        prepared.append(signals, start, end)
                        if voice is not None:
                            voice(signals, start, end)
                    def align(source, audio, text, offset):
                        try:
                            words, _ = align_chunk(aligner, audio, text, offset)
                            return words
                        finally:
                            mx.clear_cache()
                    incremental = ContextualProcessor(reader, journal, recognize, align, prepare, observe)
                    send({'capture_id': reader.capture_id, 'attached': True, 'model_loads': 1, 'aligner_loads': 1})
                    continue
                policy = json.dumps({'model_lock_sha256': hashlib.sha256(Path(__file__).with_name('qwen-model-lock.json').read_bytes()).hexdigest(),
                    'model': str(directory), 'echo_cancellation': request['echo_cancellation'],
                    'resampling': RESAMPLING_POLICY, 'block_seconds': 12, 'context_revision': 'raw-v1',
                    'language': 'Spanish', 'temperature': 0.0, 'tokens_per_second': 16,
                    'speech_detection': VAD_POLICY if request.get('speech_detection') == 'webrtc_rnn' else None,
                    'vocabulary_sha256': vocabulary_hash(terms)}, sort_keys=True)
                journal = BlockJournal(state/'incremental'/reader.capture_id/'blocks.sqlite3', reader.capture_id, policy)
                incremental = IncrementalProcessor(reader, journal, recognize, prepare)
                send({'capture_id': reader.capture_id, 'attached': True, 'model_loads': 1})
                continue
            if request.get('command') == 'poll':
                if (set(request) != {'command', 'after', 'capture_id'} or type(request['after']) is not int
                        or not 0 <= request['after'] < 2**63 or incremental is None
                        or request['capture_id'] != incremental.reader.capture_id):
                    raise ValueError('qwen_live_request_invalid')
                with contextlib.redirect_stdout(sys.stderr):
                    outcome = incremental.poll()
                    if voice is not None and outcome['state'] == 'drained':
                        voice.finish()
                    if contextual and outcome['state'] == 'drained' and not published:
                        # The native closer publishes progress before capture.json.
                        # Do not announce completion until both receipts are durable.
                        try:
                            publish(incremental.reader.folder, incremental.reader.progress(), journal, prepared, policy)
                            published = True
                        except FileNotFoundError:
                            if (incremental.reader.folder/'sources/capture.json').exists():
                                raise
                            outcome = {'state': 'waiting'}
                through = incremental.verified_context_frame if contextual else incremental.position
                events = journal.events(request['after'], through_frame=through)
                # A replay can already be drained while a large event log still
                # has pages left. Deliver all events before signalling completion.
                if outcome['state'] == 'drained' and len(events) == 64:
                    outcome = {**outcome, 'state': 'processed'}
                send({**outcome, 'events': events, 'capture_id': incremental.reader.capture_id,
                      'model_loads': 1, 'decoded_blocks': decoded_blocks, 'peak_mlx_gb': mx.get_peak_memory()/1e9})
                continue
            if set(request) != {'samples'}:
                raise ValueError('qwen_live_request_invalid')
            values = validate_samples(request['samples'])
            started = time.monotonic()
            text = ''
            with contextlib.redirect_stdout(sys.stderr):
                audio = np.asarray(values, dtype=np.float32)
                if np.any(audio):
                    limit = max(128, math.ceil(len(audio)/16000*16))
                    result = model.generate(audio, language='Spanish', max_tokens=limit,
                                            temperature=0.0, prefill_step_size=512, verbose=False)
                    if result.generation_tokens >= limit:
                        raise ValueError('qwen_live_token_limit')
                    text = result.text.strip()
                mx.clear_cache()
            send({'text': text, 'elapsed_seconds': time.monotonic()-started,
                  'audio_seconds': len(values)/16000, 'peak_mlx_gb': mx.get_peak_memory()/1e9})
    finally:
        if voice is not None:
            voice.close()
        if prepared is not None:
            prepared.close()
        if journal is not None:
            journal.close()
        os.close(lease)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        code = str(error)
        if not (code.startswith(('qwen_', 'acoustic_worker_', 'incremental_', 'consolidation_', 'echo_')) and code.replace('_','').isalnum()):
            code = 'qwen_live_failed'
        send({'error_code': code})
        sys.exit(1)
