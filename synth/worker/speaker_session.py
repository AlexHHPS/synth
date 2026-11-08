"""Bounded private pipe to a resident Core ML helper, usable under Python -I.

Only the packaged helper loads voice models. The caller verifies assets and owns
the acoustic lease. A child inherits that lease and is reaped before replacement.
No HTTP, reference identities, enrollment or browser transport exists here.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import threading
import time
from uuid import uuid4
import wave

MAX_REPLY = 2 * 1024 * 1024


def vector(value, dimensions):
    if (not isinstance(value, list) or len(value) != dimensions
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in value)):
        raise ValueError('speaker_session_embedding_invalid')
    norm = math.sqrt(sum(x*x for x in value))
    if not math.isfinite(norm) or norm < 1e-8:
        raise ValueError('speaker_session_embedding_invalid')
    return [x/norm for x in value]


class SpeakerSession:
    def __init__(self, binary, models, directory, revision, lock_fd=None, timeout=120):
        self.directory = Path(directory).resolve(strict=True)
        self.revision, self.timeout = revision, timeout
        self.closed, self.buffer = False, b''
        self.gate = threading.RLock()
        self.selector = selectors.DefaultSelector()
        started = time.monotonic()
        self.process = subprocess.Popen(
            [str(binary), 'serve', str(models), str(self.directory)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, pass_fds=() if lock_fd is None else (lock_fd,))
        os.set_blocking(self.process.stdout.fileno(), False)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            ready = self._read()
            if (ready.get('ready') is not True or ready.get('protocol_version') != 1
                    or ready.get('revision') != revision or ready.get('model_loads') != 1):
                raise ValueError('speaker_session_runtime_mismatch')
        except BaseException:
            self.close()
            raise
        self.load_seconds = time.monotonic()-started

    def _read(self):
        deadline = time.monotonic()+self.timeout
        while b'\n' not in self.buffer:
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise ValueError('speaker_session_timeout')
            incoming = os.read(self.process.stdout.fileno(), 65536)
            if not incoming:
                raise ValueError('speaker_session_interrupted')
            self.buffer += incoming
            if len(self.buffer) > MAX_REPLY:
                raise ValueError('speaker_session_output_too_large')
        line, self.buffer = self.buffer.split(b'\n', 1)
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise ValueError('speaker_session_protocol_invalid') from None
        return value

    def _exchange(self, operation, **arguments):
        with self.gate:
            if self.closed:
                raise ValueError('speaker_session_closed')
            identifier = str(uuid4())
            frame = json.dumps({'id': identifier, 'operation': operation, **arguments},
                               ensure_ascii=False, allow_nan=False).encode()+b'\n'
            if len(frame) > 1024*1024:
                raise ValueError('speaker_session_request_too_large')
            try:
                self._write(frame)
                value = self._read()
                if value.get('id') != identifier:
                    raise ValueError('speaker_session_response_mismatch')
            except OSError:
                self.close()
                raise ValueError('speaker_session_interrupted') from None
            except ValueError:
                self.close()
                raise
            code = value.get('error_code')
            if code is not None:
                raise ValueError(code if code in ('speaker_inference_failed', 'speaker_session_request_invalid')
                                 else 'speaker_session_failed')
            elapsed = value.get('elapsed_seconds')
            if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
                raise ValueError('speaker_session_output_invalid')
            return value

    def _write(self, frame):
        deadline = time.monotonic()+self.timeout
        view = memoryview(frame)
        with selectors.DefaultSelector() as writable:
            writable.register(self.process.stdin, selectors.EVENT_WRITE)
            while view:
                remaining = deadline-time.monotonic()
                if remaining <= 0 or not writable.select(remaining):
                    raise ValueError('speaker_session_timeout')
                try:
                    count = os.write(self.process.stdin.fileno(), view)
                    view = view[count:]
                except BlockingIOError:
                    continue

    def _path(self, path):
        path = Path(path).absolute()
        if path != path.resolve(strict=False) or not path.is_relative_to(self.directory):
            raise ValueError('speaker_session_path_denied')
        return path

    def _audio(self, path, maximum):
        path = self._path(path)
        with wave.open(str(path)) as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                raise ValueError('speaker_input_must_be_mono_16khz_pcm16')
            duration = audio.getnframes()/16
            if not 0 < duration <= maximum*1000:
                raise ValueError('speaker_session_audio_invalid')
        return path, round(duration)

    def embed(self, audio):
        path, duration = self._audio(audio, 10)
        if duration < 3000:
            raise ValueError('speaker_session_audio_invalid')
        value = self._exchange('embed', input=str(path))
        if value.get('duration_ms') != duration:
            raise ValueError('speaker_session_output_invalid')
        return {**value, 'embedding': vector(value.get('embedding'), 256)}

    def project(self, vectors):
        if not isinstance(vectors, list) or not 1 <= len(vectors) <= 128:
            raise ValueError('speaker_projection_invalid')
        value = self._exchange('project', vectors=[vector(v, 256) for v in vectors])
        projected = value.get('vectors')
        if (value.get('embedding_space') != 'plda_rho128_v1' or not isinstance(projected, list)
                or len(projected) != len(vectors)):
            raise ValueError('speaker_projection_invalid')
        return [vector(v, 128) for v in projected]

    def diarize(self, audio):
        path, duration = self._audio(audio, 45)
        value = self._exchange('diarize', input=str(path))
        segments = value.get('segments')
        if (value.get('speech_state') not in ('detected', 'no_usable_speech')
                or not isinstance(segments, list) or len(segments) > 4096
                or value['speech_state'] == 'no_usable_speech' and segments):
            raise ValueError('speaker_output_invalid')
        for segment in segments:
            start, end = segment.get('start_ms'), segment.get('end_ms')
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= duration+100
                    or not isinstance(segment.get('speaker_id'), str) or not 0 < len(segment['speaker_id']) <= 100):
                raise ValueError('speaker_segment_invalid')
            segment['end_ms'] = min(end, duration)
        return {**value, 'segments': sorted(segments, key=lambda s: (s['start_ms'], s['end_ms'], s['speaker_id']))}

    def embed_sealed(self, audio, key, binding, output):
        """The pipe returns a durable encrypted-file receipt, never a raw vector."""
        path, duration = self._audio(audio, 10)
        output = self._path(output)
        if duration < 3000 or output.exists():
            raise ValueError('speaker_session_audio_invalid')
        if not isinstance(key, bytes) or len(key) != 32 or not isinstance(binding, str) or not 0 < len(binding.encode()) <= 32768:
            raise ValueError('voice_binding_invalid')
        value = self._exchange('embed', input=str(path), seal={
            'key_hex': key.hex(), 'binding': binding, 'output': str(output)})
        if (set(value) != {'id', 'path', 'sha256', 'sealed', 'elapsed_seconds'}
                or value.get('path') != str(output) or value.get('sealed') is not True
                or not output.is_file() or output.stat().st_size > 1024*1024):
            self.close()
            raise ValueError('voice_sealed_output_invalid')
        self._path(output)
        with output.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if value.get('sha256') != digest:
            self.close()
            raise ValueError('voice_sealed_output_invalid')
        return value

    def close(self):
        with self.gate:
            if self.closed:
                return
            self.closed = True
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(self.process.pid, sig)
                    except ProcessLookupError:
                        pass
                    try:
                        self.process.wait(timeout=5)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            self.selector.close()
            self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
