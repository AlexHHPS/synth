# Build and installation

Current target: Apple Silicon, macOS 14+, Spanish acoustic pipeline.
Developers need full Xcode, Rust/Cargo, Node 22 with pnpm 10, Python 3.12,
CMake and rtk. Packaged users need none of those tools. This repository does not
yet publish a verified installer or a notarized release.

## Backend

Use your own dedicated Supabase project for Auth/PostgreSQL and a container host
such as Railway for the API and document worker. Configure Google Auth, allowed
domains, database roles, session function and secrets according to
[security.md](security.md) and synth/deploy/cloud.env.example. Never use a
service-role key in the client. Configure the TLS root CA appropriate for your
actual database; the included certificate is a public trust anchor, not a secret.

Build the image from the repository root:

```sh
docker build -f synth/deploy/Dockerfile -t synth-api .
```

The worker can also use `synth/deploy/Dockerfile.worker`; an optional Hermes
adapter is built from `synth/deploy/Dockerfile.hermes`. Text calls go straight to
`OMNIROUTE_URL` unless `HERMES_URL` is set. Schema v10 adds per-actor row level
security with separate API and worker roles: see [security-rls.md](security-rls.md).
To show a Google Calendar agenda, create your own OAuth client, add the
`calendar.events.readonly` scope to your Supabase Google provider and set
`SYNTH_GOOGLE_CLIENT_ID` / `SYNTH_GOOGLE_CLIENT_SECRET` on the API.

The API runs `python -m uvicorn synth.server.app:app --host 0.0.0.0 --port 18280 --no-access-log`.
The worker uses the same image and `python -m synth.worker.main`.
For isolated keys-only development, run
`python3 synth/scripts/bootstrap-secrets.py`, supply your existing gateway key in
`synth/.runtime/secrets/omniroute_api_key`, then
`docker compose -f synth/deploy/compose.yaml up --build -d`.
This development compose includes its own PostgreSQL and binds API to loopback.
For cloud use your own HTTPS domain, Supabase and runtime secrets instead.
No cloud account or paid service is automatically created by these scripts.

## Source build

```sh
git clone https://github.com/AlexHHPS/synth.git
cd synth
git submodule update --init --recursive
python3.12 -m venv synth/.toolchains/build-venv
synth/.toolchains/build-venv/bin/python -m pip install -r synth/deploy/requirements.txt -r synth/host-build-requirements.txt pyinstaller cmake uv
pnpm --dir frontend install --frozen-lockfile
python3 scripts/configure-brand.py --api-url https://voice.example.com --supabase-url https://exampleproject.supabase.co
synth/.toolchains/build-venv/bin/python synth/scripts/prepare-runtime.py
synth/.toolchains/build-venv/bin/python synth/scripts/prepare-ffmpeg.py
synth/.toolchains/build-venv/bin/python synth/scripts/download-vad-model.py
sh synth/scripts/build-native.sh
synth/.toolchains/build-venv/bin/python synth/scripts/package-host.py
synth/.toolchains/build-venv/bin/python synth/scripts/package-pilot.py --format zip
```

Replace example origins before building. The speaker runtime and models are pinned,
ASR weights are checked by SHA-256, and downloads include their notices. The build
uses upstream pinned dependencies and a source-built, audio-only FFmpeg executable.
The package includes its exact source archive, build flags and LGPL license.
Synth invokes the executable as a separate process; it does not link FFmpeg libraries.
Package-host requires the native build's FFmpeg sidecar, public model files,
standalone whisper-cli, `uv`, the Silero VAD model and the Swift acoustic executable.
The live Qwen speech runtime is not bundled: first-run setup installs its pinned,
hash-locked packages and weights into the app data folder. It never reads another
app's installed model directory, session or library.

## Install a build you trust

Verify its supplied SHA-256, unpack the ZIP and copy the app to Applications.
Start the app, sign in with your permitted Google account and grant microphone
and system audio/screen recording permissions when requested. Create a 30-second
voice profile and explicitly share it if other employees should recognize you.
The bundled local Whisper model is prepared in this brand's app-data directory
on first launch; no separate transcription-model download is required. Existing
models are preserved. The model result remains provisional pending independent calibration.

Ad hoc builds can trigger macOS security prompts. Use the operating system's
per-app approval only after verifying the origin and checksum; do not disable
Gatekeeper globally. Developer ID signing/notarization is a distributor step
outside the current source build. Keep a prior app bundle and a protected backup
of app data before updating; never replace another product's app-data namespace.

## Checks

```sh
synth/.toolchains/build-venv/bin/python -m unittest discover -s synth/tests
pnpm --dir frontend exec tsc --noEmit
pnpm --dir frontend build
python3 scripts/audit-public.py
python3 scripts/audit-public.py --index
```

Human enrollment/calibration, call capture and permissions still require a real
recording. Synthetic contract tests do not establish voice recognition accuracy
or compatibility on a second physical Mac.
