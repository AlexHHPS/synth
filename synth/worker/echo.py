"""Offline WebRTC AEC3. The system track is the render reference.

Run in the isolated local audio runtime. Never mute microphone frames by energy
or remove transcript words as a substitute for acoustic cancellation.
"""
import contextlib
import importlib.metadata
import json
from pathlib import Path
import sys
import time
import wave

VERSION = '0.2.0'
RATE = 16000


class StreamingEchoCanceller:
    """One AEC3 state per capture; full blocks keep the 10 ms frame boundary.

    Restart reconstructs this state by replaying committed source blocks before
    resuming ASR. Never reuse an instance across captures or after a failed block.
    """
    def __init__(self, delay_ms=0):
        self.delay_ms, self.processor = delay_ms, None

    def __call__(self, sources):
        if not {"microphone", "system"} <= set(sources):
            return sources
        import numpy as np
        from pywebrtc_audio import AudioProcessor
        if importlib.metadata.version('pywebrtc-audio') != VERSION:
            raise ValueError('echo_runtime_version_mismatch')
        mic, far = sources["microphone"], sources["system"]
        if (mic.ndim != 1 or far.ndim != 1 or len(mic) != len(far)
                or not np.isfinite(mic).all() or not np.isfinite(far).all()):
            raise ValueError('echo_audio_invalid')
        if self.processor is None:
            self.processor = AudioProcessor(sample_rate=RATE, echo_cancellation=True,
                noise_suppression=False, auto_gain_control=False, stream_delay_ms=self.delay_ms)
        output = self.processor.process(mic, far)
        if len(output) != len(mic) or not np.isfinite(output).all():
            raise ValueError('echo_output_invalid')
        return {**sources, "microphone": output}


def read_audio(path):
    import numpy as np
    with wave.open(str(path)) as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (RATE, 1, 2):
            raise ValueError('echo_input_must_be_mono_16khz_pcm16')
        return np.frombuffer(stream.readframes(stream.getnframes()), '<i2').astype('float32')/32768


def cancel_echo(microphone, system, delay_ms=0):
    import numpy as np
    from pywebrtc_audio import AudioProcessor
    if importlib.metadata.version('pywebrtc-audio') != VERSION:
        raise ValueError('echo_runtime_version_mismatch')
    mic = np.asarray(microphone, dtype='float32')
    far = np.asarray(system, dtype='float32')
    if mic.ndim != 1 or far.ndim != 1 or not np.isfinite(mic).all() or not np.isfinite(far).all():
        raise ValueError('echo_audio_invalid')
    # A source shorter than the microphone is silence after its end; never
    # truncate the microphone timeline to match the other source.
    far = np.pad(far[:len(mic)], (0, max(0, len(mic)-len(far))))
    if not len(mic) or not np.any(np.abs(far) > .0001):
        return mic.copy(), {'engine': 'WebRTC-AEC3', 'version': VERSION, 'skipped': 'system_silent'}
    processor = AudioProcessor(sample_rate=RATE, echo_cancellation=True,
                               noise_suppression=False, auto_gain_control=False,
                               stream_delay_ms=delay_ms)
    started = time.monotonic()
    output = processor.process(mic, far)
    if len(output) != len(mic) or not np.isfinite(output).all():
        raise ValueError('echo_output_invalid')
    return output, {'engine': 'WebRTC-AEC3', 'version': VERSION, 'delay_hint_ms': delay_ms,
                    'samples': len(mic), 'elapsed_seconds': time.monotonic()-started,
                    'high_pass_filter': True, 'noise_suppression': False, 'auto_gain_control': False}


def run(request):
    import numpy as np
    mic = read_audio(request['microphone'])
    system = read_audio(request['system'])
    output, stats = cancel_echo(mic, system)
    target = Path(request['output'])
    with wave.open(str(target), 'wb') as stream:
        stream.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        stream.writeframes(np.clip(np.rint(output*32768), -32768, 32767).astype('<i2').tobytes())
    target.chmod(0o600)
    return stats


if __name__ == '__main__':
    try:
        with contextlib.redirect_stdout(sys.stderr):
            stats = run(json.load(sys.stdin))
        print(json.dumps(stats))
    except Exception as error:
        code = str(error)
        if not (code.startswith('echo_') and code.replace('_', '').isalnum()):
            code = 'echo_processing_failed'
        print(json.dumps({'error_code': code, 'exception': type(error).__name__}))
        sys.exit(1)
