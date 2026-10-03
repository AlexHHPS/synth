# Synth repository guide

Synth is a white label meeting notebook derived from MIT-licensed Meetily Community.
Read README.md and docs/synth/installation.md before making implementation choices.

## Responsibilities

- frontend/src/synth: active workspace, public components, approved API bridge.
- frontend/src-tauri: native capture, permissions, live ASR, bundle identity.
- synth/server: central library, folder ACLs, REST/MCP, encrypted shared centroids.
- synth/worker: document jobs and version fencing; local acoustic host pipeline.
- synth/speakers: local enrollment, inference, matching and evaluation.
- synth/deploy: own-cloud configuration, restricted roles, container images.
- scripts/configure-brand.py: identity, links, visual assets and approved origins.
- backend: archived upstream backend; never deploy it as Synth's service.

## Constraints

Audio and acoustic inference stay on the Mac. The database stores text and encrypted
shared profile centroids, never recordings. Only text reaches the configured gateway.
Do not claim fully offline processing or calibrated identity accuracy. Keep company
credentials, endpoints, proprietary components and user data out of public source.
Preserve LICENSE.md, third-party notices and immutable SQL migration filenames.
Never copy proprietary Meetily Pro code or imply access to its commercial services.

## Validation

Follow the build guide for the acoustic host and native app. Run:

```sh
python -m unittest discover -s synth/tests
pnpm --dir frontend exec tsc --noEmit
pnpm --dir frontend build
python scripts/audit-public.py
python scripts/audit-public.py --index
```

Audit the proposed commit and new history before publishing. Changes to branding
must also update notifications, About, app-data namespaces and native icons.
Synthetic tests do not replace independent calibration or physical Mac tests.
