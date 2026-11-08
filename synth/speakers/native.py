"""Local Core ML adapter; never returns profile vectors through the product API."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import wave
from contextlib import contextmanager
from contextvars import ContextVar

from synth.worker.journal import file_hash
from synth.worker.subprocesses import run_process
from .profiles import normalized

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "synth/.runtime/swift-build/release/synth-speakers"
MODELS = ROOT / "synth/.runtime/models/speakers-community1"
LOCK = ROOT / "synth/resources/speaker-model-lock.json"
REVISION = "c388107348134698135cfd34f3f59dc823b6e7ce"
_VERIFIED = set()
_SCOPE = ContextVar('speaker_inference_scope', default=None)
# Embeddings are taken from 10 s crops: 5 s crops are padded inside the model's 10 s window and
# their vectors are no longer speaker-discriminative (different speakers ≈ 0.6 cosine).
CROP_SAMPLES = 160_000
EMBEDDING_PROFILE = "crop10s"


def resident_session(working_directory, binary=BINARY, models=MODELS, lock_fd=None):
    """Same verified voice models and crop semantics, kept resident for a capture."""
    from synth.worker.speaker_session import SpeakerSession
    if not Path(binary).is_file():
        raise ValueError('speaker_binary_missing')
    verify_models(models)
    return SpeakerSession(binary, models, working_directory, REVISION, lock_fd=lock_fd)


@contextmanager
def reuse_models(working_directory, lock_fd=None):
    """One lazy helper per bounded acoustic operation, isolated between callers."""
    if _SCOPE.get() is not None:
        raise ValueError('speaker_session_scope_nested')
    state = {'root': Path(working_directory).resolve(strict=True), 'lock_fd': lock_fd, 'worker': None, 'configuration': None}
    token = _SCOPE.set(state)
    try:
        yield state
    finally:
        _SCOPE.reset(token)
        if state['worker'] is not None:
            state['worker'].close()


def _scoped_worker(binary, models, working_directory, lock_fd):
    state = _SCOPE.get()
    if state is None:
        return None
    if lock_fd != state['lock_fd'] or not Path(working_directory).resolve(strict=True).is_relative_to(state['root']):
        raise ValueError('speaker_session_scope_mismatch')
    configuration = (str(Path(binary).resolve()), str(Path(models).resolve()))
    if state['worker'] is None:
        state['worker'] = resident_session(state['root'], binary=binary, models=models, lock_fd=lock_fd)
        state['configuration'] = configuration
    elif state['configuration'] != configuration:
        raise ValueError('speaker_session_scope_mismatch')
    return state['worker']


def embedding_model_id(lock=None):
    """Model revision + crop policy: vectors from different crop policies are never compared."""
    return json.loads(Path(lock or LOCK).read_text())["model_revision"] + "#" + EMBEDDING_PROFILE


def verify_models(root=MODELS):
    root = Path(root)
    manifest = json.loads(LOCK.read_text())["model_manifest"]
    for item in manifest["files"]:
        path = root / item["path"]
        if not path.is_file():
            raise ValueError("speaker_model_missing")
        stat = path.stat()
        signature = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, item["sha256"])
        if signature not in _VERIFIED:
            if stat.st_size != item["size"] or file_hash(path) != item["sha256"]:
                raise ValueError("speaker_model_hash_mismatch")
            _VERIFIED.add(signature)


def infer(operation, audio, working_directory, binary=BINARY, models=MODELS, lock_fd=None):
    if operation not in {"diarize", "embed"}:
        raise ValueError("speaker_operation_invalid")
    audio, binary, models = Path(audio), Path(binary), Path(models)
    scope = _SCOPE.get()
    if scope is None:
        if not binary.is_file():
            raise ValueError('speaker_binary_missing')
        verify_models(models)
    elif (lock_fd != scope['lock_fd'] or not audio.resolve().is_relative_to(scope['root'])
            or not Path(working_directory).resolve().is_relative_to(scope['root'])):
        raise ValueError('speaker_session_scope_mismatch')
    with wave.open(str(audio)) as stream:
        if stream.getnchannels() != 1 or stream.getframerate() != 16000 or stream.getsampwidth() != 2:
            raise ValueError("speaker_input_must_be_mono_16khz_pcm16")
        duration_ms = round(stream.getnframes() / 16)
    directory = Path(working_directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    started = time.monotonic()
    worker = _scoped_worker(binary, models, directory, lock_fd)
    if worker is not None:
        result = worker.embed(audio) if operation == 'embed' else worker.diarize(audio)
        return {**result, 'mode': 'real', 'revision': REVISION, 'audio_sha256': file_hash(audio),
                'duration_ms': duration_ms, 'elapsed_seconds': time.monotonic()-started,
                'model_revision': json.loads(LOCK.read_text())['model_revision']}
    # Embeddings exist briefly in a private temporary directory and are then erased.
    # This directory is inside the caller's managed raw/temporary retention tree.
    with tempfile.TemporaryDirectory(prefix="speaker-", dir=directory) as temporary:
        output = Path(temporary) / "result.json"
        try:
            process = run_process([str(binary), operation, str(audio), str(models), str(output)],
                                     capture_output=True, timeout=min(4000, duration_ms / 1000 * 3 + 120),
                                     pass_fds=() if lock_fd is None else (lock_fd,))
        except subprocess.TimeoutExpired:
            raise ValueError("speaker_inference_timeout") from None
        if process.returncode or not output.is_file():
            raise ValueError("speaker_inference_failed")
        result = json.loads(output.read_text())
    if result.get("mode") != "real" or result.get("revision") != REVISION:
        raise ValueError("speaker_runtime_mismatch")
    if operation == "embed":
        result["embedding"] = normalized(result.get("embedding", []))
    else:
        segments = result.get("segments")
        if not isinstance(segments, list):
            raise ValueError("speaker_output_invalid")
        if result.get("speech_state", "detected") not in {"detected", "no_usable_speech"}:
            raise ValueError("speaker_output_invalid")
        if result.get("speech_state") == "no_usable_speech" and segments:
            raise ValueError("speaker_output_invalid")
        for segment in segments:
            start, end = segment.get("start_ms"), segment.get("end_ms")
            if (not isinstance(start, int) or not isinstance(end, int) or
                    not 0 <= start < end <= duration_ms + 100 or
                    not isinstance(segment.get("speaker_id"), str) or not segment["speaker_id"]):
                raise ValueError("speaker_segment_invalid")
            segment["end_ms"] = min(end, duration_ms)
        result["segments"] = sorted(segments, key=lambda s: (s["start_ms"], s["end_ms"], s["speaker_id"]))
    result.update({"audio_sha256": file_hash(audio), "duration_ms": duration_ms,
                   "elapsed_seconds": time.monotonic() - started,
                   "model_revision": json.loads(LOCK.read_text())["model_revision"]})
    return result


def project_embeddings(vectors, working_directory, binary=BINARY, models=MODELS, lock_fd=None):
    """Whiten raw centroids/windows in private memory/files; never rewrite profiles."""
    vectors = [normalized(vector) for vector in vectors]
    if not vectors:
        return []
    worker = _scoped_worker(binary, models, working_directory, lock_fd)
    if worker is not None:
        return [v for offset in range(0, len(vectors), 128) for v in worker.project(vectors[offset:offset+128])]
    if not Path(binary).is_file():
        raise ValueError('speaker_binary_missing')
    verify_models(models)
    projected = []
    with tempfile.TemporaryDirectory(prefix='projection-',dir=working_directory) as temporary:
        source, target = Path(temporary)/'input.json', Path(temporary)/'result.json'
        for offset in range(0,len(vectors),128):
            selected = vectors[offset:offset+128]
            source.write_text(json.dumps(selected));source.chmod(0o600)
            target.unlink(missing_ok=True)
            try:
                process = run_process([str(binary),'project',str(source),str(models),str(target)],
                                         capture_output=True,timeout=30,
                                         pass_fds=() if lock_fd is None else (lock_fd,))
            except subprocess.TimeoutExpired:
                raise ValueError('speaker_projection_timeout') from None
            if process.returncode or not target.is_file() or target.stat().st_size > 1024*1024:
                raise ValueError('speaker_projection_failed')
            result = json.loads(target.read_text())
            if (result.get('mode') != 'real' or result.get('revision') != REVISION or
                    result.get('embedding_space') != 'plda_rho128_v1' or
                    not isinstance(result.get('vectors'),list) or len(result['vectors']) != len(selected)):
                raise ValueError('speaker_projection_invalid')
            projected.extend(normalized(vector,128) for vector in result['vectors'])
    return projected


def _label_segments(transcript, diarization):
    result = json.loads(json.dumps(transcript))
    result.pop('words', None)
    for segment in result["segments"]:
        segment["speaker_id"] = None
        segment["employee_id"] = None
        evidence = {}
        for turn in diarization["segments"]:
            overlap = max(0, min(segment["end_ms"], turn["end_ms"]) - max(segment["start_ms"], turn["start_ms"]))
            if overlap:
                evidence[turn["speaker_id"]] = evidence.get(turn["speaker_id"], 0) + overlap
        duration = segment["end_ms"] - segment["start_ms"]
        if len(evidence) == 1 and duration > 0:
            speaker, overlap = next(iter(evidence.items()))
            if overlap / duration >= 0.5:
                segment["speaker_id"] = speaker
    return result


def attach_speaker_labels(transcript, diarization, words=None):
    """Keep confident phrase labels; resolve ambiguous phrases using word times.

    Words within overlapping voices or without acoustic evidence stay unknown.
    The original phrase-level coverage rule remains for already resolved phrases.
    """
    result = _label_segments(transcript, diarization)
    if not words:
        return result
    if ' '.join(w['text'] for w in words).split() != ' '.join(s['text'] for s in result['segments']).split():
        raise ValueError('speaker_word_alignment_mismatch')
    previous = -1
    for word in words:
        start, end = word.get('start_ms'), word.get('end_ms')
        if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int)
                or not 0 <= start <= end <= result['duration_ms'] or start < previous):
            raise ValueError('speaker_word_alignment_invalid')
        previous = start
    labelled_words = _label_segments({**result, 'segments': [{**w, 'id': str(i)} for i, w in enumerate(words)]}, diarization)['segments']
    rebuilt, cursor = [], 0
    for phrase in result['segments']:
        selected, tokens = [], []
        expected = phrase['text'].split()
        while len(tokens) < len(expected) and cursor < len(labelled_words):
            word = labelled_words[cursor]
            selected.append(word)
            tokens.extend(word['text'].split())
            cursor += 1
        if tokens != expected:
            raise ValueError('speaker_word_alignment_mismatch')
        if phrase['speaker_id'] is not None or not any(w['speaker_id'] is not None for w in selected):
            rebuilt.append(phrase)
            continue
        grouped = []
        for word in selected:
            if (grouped and grouped[-1]['speaker_id'] == word['speaker_id']
                    and word['start_ms'] - grouped[-1]['end_ms'] <= 600):
                grouped[-1]['text'] += ' ' + word['text']
                grouped[-1]['end_ms'] = max(grouped[-1]['end_ms'], word['end_ms'])
            else:
                grouped.append(dict(word))
        # Word-level diarization can split a phrase, but its ASR uncertainty and
        # original window reference still belong to every resulting fragment.
        metadata = {k: phrase[k] for k in ('asr_block_id', 'evidence_state') if k in phrase}
        fields = ('id', 'text', 'start_ms', 'end_ms', 'source_id', 'speaker_id', 'employee_id')
        rebuilt.extend({**{k: word[k] for k in fields if k in word}, **metadata} for word in grouped)
    for i, segment in enumerate(rebuilt):
        segment['id'] = f's{i:06d}'
    result['segments'] = rebuilt
    return result
