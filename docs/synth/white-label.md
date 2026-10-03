# White label builds

The default product is Synth: its own mark, blue accent, app identifier
`dev.synth.voice`, app data and Keychain namespace. The acoustic control service
uses loopback port `18383`; the library may use local `18280` for development or
an operator-owned HTTPS cloud origin. There is no company backend in this source.

Configure your backend and optional Google sign-in **before building**:

```sh
python3 scripts/configure-brand.py --name "Example Voice" \
  --identifier dev.example.voice --accent "160 70% 35%" \
  --logo /example-mark.svg --generate-icons --api-url https://voice.example.com \
  --supabase-url https://exampleproject.supabase.co
```

Supply your own square PNG or SVG logo in `frontend/public`. `--generate-icons`
updates native bundle icons and the web favicon from that same asset (frontend
dependencies must be installed).
The script synchronizes the product metadata, UI tokens, native destination
allowlist, identifier, host app-data and Keychain names. Rebuild both the native
app and the packaged host; mixing brands or deployments is unsupported.
The JS interface cannot choose another destination for credentials.
A hostname is configuration, not a secret. Never put database passwords,
service-role keys, integration keys or OAuth client secrets in branding files.

For local development, restore the default configuration with
`python3 scripts/configure-brand.py`. For a central deployment, retain your own
HTTPS API and Supabase origins. Backend Auth settings are configured separately
in the server environment. Existing installs require a deliberate migration of
backend-config.json if the approved destination changes; session and library
migration must not happen implicitly.
