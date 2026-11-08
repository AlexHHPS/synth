"""Voice keys use the same Keychain account in host and isolated runtime."""
from synth.worker.voice_key import SERVICE, read, write
from synth.worker.voice_key import obtain as _obtain


def obtain(legacy, account="synth-voice"):
    # Explicit dependencies preserve migration checks and test injection.
    return _obtain(legacy, account, read_key=read, write_key=write)
