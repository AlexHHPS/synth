# Security and privacy

Use a dedicated deployment per organization. Production requires HTTPS, a private
PostgreSQL endpoint with certificate verification, a runtime database role with
only required privileges, and a separately controlled migration role. Do not
expose database ports or bootstrap administration endpoints to untrusted networks.
For Supabase use `DATABASE_SCHEMA=synth_voice` consistently. Create a dedicated
login role named `synth_voice_backend` with a random password managed outside source
(no schema ownership, no BYPASSRLS). Using project management credentials, apply
`synth/deploy/supabase-private.sql` first to create the schema and registration hook.
Run `python -m synth.server.migrations` with management credentials and the desired
`ALLOWED_DOMAINS`, then reapply the private script to protect the newly created tables.
Apply `synth/deploy/supabase-runtime.sql` and `synth/deploy/supabase-session.sql`.
Switch API and worker to the backend role and `SYNTH_RUN_MIGRATIONS=0`.
The backend-only RLS policies permit this role to enforce application folder ACLs;
browser roles have neither policies nor grants. The role cannot change schema or
read Auth tables directly. Domain changes require rerunning management migrations.
Review database grants, backups and Auth hooks for your own deployment; these
are operator responsibilities, not automatically provisioned by this repository.

For Google sign-in configure a Web Application OAuth client in Supabase, exact
allowed domains in ALLOWED_DOMAINS, confirmed email checks and the registration
hook from synth/deploy/supabase-private.sql. Add the exact loopback callback pattern
`http://127.0.0.1:18383/auth/callback/*` to Supabase redirect URLs. Configure
SUPABASE_CHECK_SESSIONS=1 and the session function to honor logout immediately.
Keys-only mode is for isolated development; do not use human keys to bypass SSO.

Keep bootstrap credentials, DB passwords, HERMES_API_KEY, gateway credentials and
VOICE_PROFILE_ENCRYPTION_KEY outside source, client bundles and logs. The latter
must be a stable, securely backed-up key: losing it makes saved centroids unreadable.
Back up text and encrypted profiles with the server key handled separately, and
verify restoration in a disposable database before relying on backups.

Sharing profiles requires consent. Shared profiles are visible to authorized
employees of that deployment; integration keys cannot access them. Revocation
prevents later catalog reads, but cannot claw back data already downloaded by a
client. Account offboarding must revoke access and remove its shared profile.
Voice matching is provisional and must abstain on insufficient evidence. Never
use it as login, authorization, proof of attendance or a calibrated identity claim.

Audio stays on the Mac and is removed after analysis, including enrollment
failures. The active host expires interrupted recordings within 24 hours. Text is
sent to the configured LLM provider, which may be external. Audit its retention
and provider routing separately. The inherited product's telemetry and updater
are disabled; this distribution has no vendor telemetry key or update endpoint.

Packaged builds currently use an ad hoc signature. Developer ID, hardened runtime,
notarization and stapling must be performed by your distributor before a signed
production release. Do not disable Gatekeeper globally to install this software.
