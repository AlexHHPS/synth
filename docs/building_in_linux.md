# Linux development status

Synth's supported acoustic pipeline targets Apple Silicon/macOS 14+, using Metal
for Whisper and Core ML for speaker inference. See [installation](synth/installation.md).
Inherited Linux acceleration code remains available for development, but a complete
Synth build or installer on Linux has not been validated.

## Native audio capture

The desktop capture code incorporates Meetily Community v0.5.0's ALSA and
PulseAudio support. Install the PulseAudio and ALSA development libraries
(`libpulse-dev` and `libasound2-dev` on Debian and Ubuntu) before compiling.
Capturing system audio requires a running PulseAudio server or PipeWire with
its PulseAudio compatibility service (`pipewire-pulse`). Device selection uses
the server's stable device IDs and displays its human-readable labels. When
the server is unavailable, ALSA provides a microphone-only fallback.
