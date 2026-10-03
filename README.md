# Synth

<img src="frontend/public/synth-mark.png" width="72" alt="Synth" />

An open source, white label meeting notebook with a native Mac recorder and a
central library you operate. Built on the MIT-licensed Meetily Community Edition.

- Record a quick note, an in-person conversation or a remote call without a bot.
- Capture microphone and system audio separately and retain source timestamps.
- Transcribe and diarize on Apple Silicon; compare voices against enrolled profiles.
- Keep meetings private and share folders explicitly with colleagues and integrations.
- Generate Markdown and JSON notes with segment references through your configured LLM gateway.
- Consume transcripts, notes, summaries, versions and processing state through REST and MCP.
- Use Google sign-in through your own Supabase project, with allowed email domains.

Audio and acoustic inference stay on the Mac. The central database stores text and
encrypted voice centroids, never voice recordings. Voice enrollment and sharing
require explicit consent. Recognition remains a provisional candidate until you
calibrate it independently; it is not an authentication mechanism.

## Start here

[Build and installation](docs/synth/installation.md) ·
[White label configuration](docs/synth/white-label.md) ·
[Cloud architecture](docs/synth/architecture.md) ·
[API and MCP](docs/synth/integrations.md) ·
[Privacy and security](docs/synth/security.md)

The current acoustic pipeline targets **Apple Silicon, macOS 14+ and Spanish**.
Inherited upstream Windows/Linux code is present but the complete Synth pipeline
is not validated on those platforms. This repository provides source and build
instructions; an installer release, Developer ID signing and notarization are not
currently provided. Model downloads and a configured backend are required.

## License and attribution

Synth retains the original [MIT license and copyright](LICENSE.md).
Meetily Community Edition was created by Zackriya Solutions. Native model and
runtime dependencies have their own licenses; see [third-party notices](NOTICE.md).
Synth has its own branding and is not an official Meetily release.
