# Third-party notices

Synth derives from Meetily Community Edition, copyright (c) 2024 Zackriya
Solutions, under the MIT License in LICENSE.md. This notice and the complete
license must accompany redistributions of its substantial source or binaries.

The inherited code also uses whisper.cpp, Screenpipe and transcribe-rs. Preserve
license notices in their source and dependencies; consult Cargo.lock for the
versions included in a build.

The speaker runtime uses FluidAudio at the revision pinned in
synth/speakers/native/Package.swift, under the MIT License reproduced in
[FluidAudio-LICENSE.txt](docs/synth/third-party/FluidAudio-LICENSE.txt).
The scoped Community-1 Core ML models are attributed to pyannote, WeSpeaker,
BUT Speech@FIT and Fluid Inference. Read
[Community1-NOTICE.md](docs/synth/third-party/Community1-NOTICE.md) and the pinned
model lock in synth/resources/speaker-model-lock.json. This does not grant a
license for unrelated legacy models in the same upstream repository.

Whisper model weights use their upstream MIT terms. Packaged builds include
whisper.cpp's license, speaker notices/provenance and the license and build
configuration reported by the exact FFmpeg executable they distribute. FFmpeg
licensing depends on its build options; distributors must satisfy those terms,
including applicable source availability obligations. Do not assume every FFmpeg
binary is licensed identically.

Synth's frontend controls in frontend/src/synth/design-system are independently
implemented with public open source React, Radix and Tailwind components.

The Apple Silicon Synth package uses a pinned audio-only FFmpeg 8.0 build under
LGPL-2.1-or-later, invoked as a separate executable. Its corresponding unmodified
source archive, build recipe and complete license are included under Resources/Notices.
See https://ffmpeg.org/legal.html. Other platform builds require a separate licensing review.
