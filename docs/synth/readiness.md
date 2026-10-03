# Synth readiness and remaining release work

The repository provides a single-organization product with its own brand and
operator-configured infrastructure. It contains no company deployment or credentials.

| Capability | Implementation and verification |
| --- | --- |
| Brand | Own Synth mark, UI tokens, bundle identifier, app-data and Keychain namespace. An alternate brand and approved cloud origin were compiled. |
| Capture | Native microphone/system audio, live transcript, quick notes, pause/resume and call-detection prompts. Actual calls and OS permissions require human testing. |
| Acoustic pipeline | Packaged local ASR, diarization and 256-dimensional embeddings. Synthetic acoustic smoke passes; this does not measure human recognition accuracy. |
| Voice onboarding | Local enrollment, encrypted profiles, regeneration and explicit sharing/revocation. |
| Cross-device catalog | Server stores AES-GCM encrypted centroids; authorized clients compare locally. Real second-device testing remains external. |
| Library | Central text, immutable versions, private meetings and explicitly shared folders. |
| Processing | Resumable stages, current-version fencing, transcript quality gate and trusted live-transcript recovery. |
| Documents | Markdown/JSON generation through an operator gateway, source references and bounded repair. Automated contracts pass; the API smoke seeds a synthetic document rather than calling an LLM. |
| REST/MCP | 43 HTTP/official MCP SDK checks: authenticated reads, scope isolation, versions, denial without a key, write denial and revocation. |
| Cloud Auth | Supabase Google login, allowed domains and active-session checks. A new operator deployment needs its own OAuth configuration and live login test. |
| Database permissions | Isolated PostgreSQL test verifies backend DML, denial of DDL/Auth-table access, and denial of direct browser library reads. |
| Packaging | Apple Silicon build, packaged host self-check and ad hoc signature. Distribution uses the pinned audio-only FFmpeg source build with its corresponding source and notices. |

Before an organizational release, configure the operator's cloud origins and secrets,
initialize the schema with management credentials, deploy API and worker with the
restricted backend role, and verify HTTPS, Google login and a document generated
through the chosen gateway. Publish only an audited source commit and installer
checksum. Developer ID signing/notarization is a separate distributor step.

Before relying on voice names, calibrate with independent recordings and accept the
metrics in [voice-calibration.md](voice-calibration.md). Synthetic fixtures do not
close this requirement. Test the installer and a real call on a second physical Mac
when available. No automatic updater or multi-tenant hosting is claimed.
