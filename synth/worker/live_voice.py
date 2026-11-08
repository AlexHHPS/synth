"""Bounded voice observations from prepared audio while Qwen transcribes.

The helper shares the acoustic lease, never loads Qwen or fetches profiles.
Only authenticated ciphertext and source-time provenance survive each window.
Native cluster labels are scoped to that window, never reused as identities.
"""
from array import array
import hashlib
import json
import os
from pathlib import Path
import tempfile
import wave

from .consolidated_capture import atomic_json, file_hash
from .speaker_session import SpeakerSession
from .voice_binding import binding, parse, RATE
from .voice_windows import voice_windows

REVISION = 'c388107348134698135cfd34f3f59dc823b6e7ce'
WINDOW = 30 * RATE
POLICY = 'source_voice30_guard120_first_two5_and_join10_v1'


def load_owner(folder, capture_id):
    path = folder/'capture-owner.json'
    if path.is_symlink() or path.stat().st_size > 4096:
        raise ValueError('voice_capture_owner_invalid')
    owner = json.loads(path.read_text())
    if (not isinstance(owner, dict) or set(owner) != {'version', 'capture_id', 'identity'}
            or owner['version'] != 1 or owner['capture_id'] != capture_id):
        raise ValueError('voice_capture_owner_invalid')
    # Reuse the exact canonical owner validation, without emitting an observation.
    binding(capture_id, owner['identity'], 'system', 'a'*40, 'b'*64, 5, [[0, 80000]], 'c'*64)
    return owner['identity']


def write_audio(path, pcm):
    with os.fdopen(os.open(path, os.O_CREAT|os.O_EXCL|os.O_RDWR, 0o600), 'w+b') as stream:
        with wave.open(stream, 'wb') as audio:
            audio.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
            audio.writeframes(pcm.tobytes())


def verify_assets(configuration):
    if (not isinstance(configuration, dict) or set(configuration) != {'binary', 'models', 'lock'}
            or any(not isinstance(value, str) for value in configuration.values())):
        raise ValueError('voice_runtime_config_invalid')
    binary, models, lock = (Path(configuration[name]).resolve(strict=True) for name in ('binary', 'models', 'lock'))
    content = lock.read_bytes()
    value = json.loads(content)
    if value['fluid_audio_revision'] != REVISION:
        raise ValueError('voice_runtime_revision_invalid')
    for item in value['model_manifest']['files']:
        path = models/item['path']
        if (not path.is_relative_to(models) or path != path.resolve(strict=True)
                or not path.is_file() or path.stat().st_size != item['size']
                or file_hash(path) != item['sha256']):
            raise ValueError('voice_model_hash_mismatch')
    return binary, models, value['model_revision'], hashlib.sha256(content).hexdigest()


class LiveVoiceObserver:
    def __init__(self, folder, capture_id, state, configuration, lock_fd, *, assets=verify_assets,
                 key_provider=None, factory=SpeakerSession):
        self.folder = Path(folder).resolve(strict=True)
        self.capture_id, self.owner = capture_id, load_owner(self.folder, capture_id)
        self.configuration, self.lock_fd = configuration, lock_fd
        self.assets, self.key_provider, self.factory = assets, key_provider, factory
        self.directory = self.folder/'voice-context-v1'
        self.directory.mkdir(mode=0o700, exist_ok=True)
        if self.directory != self.directory.resolve(strict=True):
            raise ValueError('voice_capture_path_invalid')
        self.location = Path(state)/'incremental'/capture_id/'voice-capture.json'
        self.location.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.start, self.position, self.buffers = 0, 0, {}
        self.worker = self.key = self.model = self.model_lock = None
        self.runtime_sha256 = None
        self.implementation_sha256 = {name: file_hash(Path(__file__).with_name(name)) for name in
            ('live_voice.py', 'voice_windows.py', 'speaker_session.py', 'voice_binding.py')}
        self.state, self.error_code, self.closed = 'collecting', None, False
        self.phase = 'collecting'
        self.completed_windows = self.embedding_calls = self.diarization_calls = self.reused_windows = 0
        try:
            self._publish_location()
        except OSError:
            pass

    def _publish_location(self):
        atomic_json(self.location, {'version': 1, 'capture_id': self.capture_id, 'owner': self.owner,
            'folder': str(self.folder), 'policy': POLICY, 'state': self.state, 'error_code': self.error_code,
            'phase': self.phase,
            'through_frame': self.start, 'sample_rate': RATE, 'completed_windows': self.completed_windows,
            'reused_windows': self.reused_windows, 'embedding_calls': self.embedding_calls,
            'diarization_calls': self.diarization_calls})

    def _runtime(self):
        if self.worker is None:
            binary, models, self.model, self.model_lock = self.assets(self.configuration)
            self.runtime_sha256 = file_hash(binary)
            if self.key_provider is None:
                from .voice_key import obtain
                self.key = obtain(self.location.parents[3]/'voice/vault-key')
            else:
                self.key = self.key_provider()
            self.worker = self.factory(binary, models, self.directory, REVISION, lock_fd=self.lock_fd)
        return self.worker

    def _abstain(self, error):
        code = str(error)
        self.state = 'unavailable'
        self.error_code = code if code.startswith(('speaker_', 'voice_')) and code.replace('_', '').isalnum() and len(code) <= 100 else 'voice_observation_failed'
        self.buffers.clear()
        if self.worker is not None:
            self.worker.close()
            self.worker = None
        self.key = None
        try:
            self._publish_location()
        except OSError:
            pass

    def __call__(self, signals, start, end):
        if self.closed or self.state == 'unavailable':
            return
        try:
            import numpy as np
            if (start % 3 or start//3 != self.position or end <= start or end-start > 3*48000
                    or set(signals)-{'microphone', 'system'} or not signals
                    or self.buffers and set(signals) != set(self.buffers)):
                raise ValueError('voice_capture_clock_invalid')
            for source, signal in signals.items():
                pcm = np.clip(np.rint(signal*32768), -32768, 32767).astype('<i2')
                if pcm.ndim != 1 or len(pcm) != (end-start+2)//3:
                    raise ValueError('voice_capture_clock_invalid')
                self.buffers.setdefault(source, array('h')).frombytes(pcm.tobytes())
            self.position += (end-start+2)//3
            if self.position-self.start >= WINDOW:
                self._window(self.position)
        except Exception as error:
            # Voice is optional evidence. A failure must not lose the ASR slab.
            self._abstain(error)

    def _window(self, end):
        if load_owner(self.folder, self.capture_id) != self.owner:
            raise ValueError('voice_capture_owner_changed')
        root = self.directory/(str(self.start)+'-'+str(end))
        root.mkdir(mode=0o700, exist_ok=True)
        if root != root.resolve(strict=True):
            raise ValueError('voice_capture_path_invalid')
        descriptor = {'capture_id': self.capture_id, 'owner': self.owner, 'policy': POLICY,
            'start_frame': self.start, 'end_frame': end,
            'sources': {s: hashlib.sha256(a.tobytes()).hexdigest() for s, a in self.buffers.items()}}
        path = root/'window.json'
        if path.is_symlink():
            raise ValueError('voice_capture_path_invalid')
        saved = json.loads(path.read_text()) if path.exists() else None
        if saved is not None and saved['input'] != descriptor:
            raise ValueError('voice_capture_input_changed')
        # Even a cached run verifies the current model assets/policy.
        self._runtime()
        if saved is not None and (saved['model'] != self.model or saved['model_lock_sha256'] != self.model_lock
                or saved.get('runtime_sha256') != self.runtime_sha256
                or saved.get('implementation_sha256') != self.implementation_sha256):
            raise ValueError('voice_capture_model_changed')
        record = saved or {'version': 1, 'input': descriptor, 'model': self.model,
            'model_lock_sha256': self.model_lock, 'runtime_sha256': self.runtime_sha256,
            'implementation_sha256': self.implementation_sha256, 'state': 'pending', 'sources': {}}
        for source, pcm in sorted(self.buffers.items()):
            value = record['sources'].get(source)
            with tempfile.TemporaryDirectory(prefix='voice-crops-', dir=root) as temporary:
                scratch = Path(temporary)
                if value is None:
                    audio = scratch/'source.wav'
                    write_audio(audio, pcm)
                    self.diarization_calls += 1
                    self.phase = 'diarization'
                    diarization = self.worker.diarize(audio)
                    turns = diarization['segments']
                    groups = []
                    for speaker in sorted({s['speaker_id'] for s in turns}):
                        windows, selection = voice_windows(pcm, turns, speaker)
                        # Two 5 s windows plus their exact concatenation: same
                        # lineage, not three independent votes. Bounded per 30 s.
                        windows = windows[:2]
                        ranges = [[[self.start+round(a*16), self.start+round(b*16)] for a,b in e['source_ranges_ms']]
                                  for _,e in windows]
                        groups.append({'id': source+':'+str(self.start)+':'+speaker,
                            'speaker_id': speaker, 'selection': selection, 'windows': ranges,
                            'observations': [], 'rejected_crops': []})
                    value = {'segments': turns, 'groups': groups}
                    record['sources'][source] = value
                    atomic_json(path, record)  # successful diarization survives a restart
                for group in value['groups']:
                    ranges = group['windows']
                    if len(ranges) != 2:
                        continue
                    for index, spans in enumerate([*ranges, [r for w in ranges for r in w]]):
                        seconds = 10 if index == 2 else 5
                        chunk = array('h')
                        for a,b in spans:
                            chunk.extend(pcm[a-self.start:b-self.start])
                        raw_hash = hashlib.sha256(chunk.tobytes()).hexdigest()
                        aad = binding(self.capture_id, self.owner, source,
                            self.model+('#crop10s' if seconds == 10 else ''), self.model_lock, seconds, spans, raw_hash)
                        identifier = hashlib.sha256(aad.encode()).hexdigest()
                        if any(o['id'] == identifier for o in group['observations']) or identifier in group['rejected_crops']:
                            continue
                        output = root/(identifier+'.sealed.json')
                        if output.exists():
                            # Native atomic write may precede journal commit.
                            envelope = json.loads(output.read_text())
                            if envelope.get('binding') != aad:
                                raise ValueError('voice_capture_binding_changed')
                            receipt = {'path': str(output), 'sha256': file_hash(output)}
                        else:
                            audio = scratch/(identifier+'.wav')
                            write_audio(audio, chunk)
                            self.embedding_calls += 1
                            self.phase = 'embedding'
                            try:
                                receipt = self.worker.embed_sealed(audio, self.key, aad, output)
                            except ValueError as error:
                                if str(error) != 'speaker_inference_failed':
                                    raise
                                group['rejected_crops'].append(identifier)
                                atomic_json(path, record)
                                continue
                        group['observations'].append({'id': identifier, 'binding': aad,
                            'descriptor': {k: receipt[k] for k in ('path', 'sha256')}})
                        atomic_json(path, record)
        for value in record['sources'].values():
            for group in value['groups']:
                for observation in group['observations']:
                    sealed = Path(observation['descriptor']['path'])
                    if (sealed != sealed.resolve(strict=True) or not sealed.is_relative_to(root)
                            or file_hash(sealed) != observation['descriptor']['sha256']):
                        raise ValueError('voice_capture_observation_changed')
        if record['state'] == 'complete':
            self.reused_windows += 1
        record['state'] = 'complete'
        atomic_json(path, record)
        self.completed_windows += 1
        self.start = end
        self.buffers = {}
        self._publish_location()

    def finish(self):
        if self.closed or self.state == 'unavailable':
            return
        try:
            if self.position > self.start:
                self._window(self.position)
            self.state = 'drained'
            self._publish_location()
        except Exception as error:
            self._abstain(error)

    def close(self):
        if self.worker is not None:
            self.worker.close()
            self.worker = None
        self.key = None
        self.buffers.clear()
        self.closed = True
