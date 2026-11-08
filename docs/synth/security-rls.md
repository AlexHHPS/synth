# Row level security per actor (schema v10)

Up to v9 the backend connected as the table owner, so PostgreSQL row level
security did not apply and isolation depended only on API checks
(`authorize_folder`, `authorize_meeting`). From v10 PostgreSQL repeats those
checks: an endpoint that forgot a check still cannot return rows the actor may
not see.

## Roles

| Role | Used by | Can do |
|---|---|---|
| `synth_voice_backend` | Migrations only (`MIGRATION_DATABASE_URL` of the API service) | Owner: creates and alters tables, functions and policies. |
| `synth_voice_api` | API service (`DATABASE_URL`) | Only what endpoints use, filtered by policies for the actor. Never deletes meetings, writes minutes, reads the audit log or creates objects. |
| `synth_voice_worker` | Document worker (`DATABASE_URL`) | Processes everyone's meetings, so its policies do not filter by actor; its grants exclude keys, identities, voice profiles and folder sharing. |
| `anon`, `authenticated` | Supabase browser roles | Nothing, not even USAGE on the schema. |

Hermes never connects to the database.

## How the actor is applied

Each request opens its own connection. After authentication `bind_actor`
(`synth/server/db.py`) sets `synth.actor` and, for integrations, `synth.key_id`.
`rls_actor`, `rls_user`, `rls_folder` and `rls_meeting` repeat the `authorize_*`
logic and re-read the principal and key on every query, so a revoked employee,
revoked key or key whose issuer left loses access immediately.

- **Private library:** visible only to its owner.
- **Shared folders:** readers read, editors also write; only the owner shares,
  changes members or deletes the folder.
- **Integrations:** read only their folders while key, principal and issuer stay active.
- **Questions:** readable only by their author.
- **Voice profiles:** readable by every employee (for local comparison), writable
  only by their owner, never visible to integrations.
- **Organisation glossary:** readable by employees; integrations read approved
  terms only. Organisation terms never store transcript excerpts.

## Known residual risks

- The API declares the actor. RLS protects against endpoint logic errors, not
  against code running inside the API container, which also holds the owner URL.
- Identity tables (`principals`, `access_keys`, `auth_identities`) are not
  filtered by actor for the API: they are read before the actor is known.
- Any employee can edit the organisation glossary, which feeds everyone's minutes.
- There are no per-employee usage limits.
- Revoking a person still requires disabling them in your identity provider and
  setting `principals.revoked_at`.

## Rollout

1. As the project operator, run `synth/deploy/supabase-roles.sql`, then set
   passwords for `synth_voice_api` and `synth_voice_worker` outside the
   repository (for example with `ALTER ROLE … PASSWORD` sent as a SCRAM verifier).
2. Deploy the API with `MIGRATION_DATABASE_URL` (owner) and `DATABASE_URL`
   (`synth_voice_api`); it migrates to v10 and starts serving. Check `/health`.
3. Deploy the worker with `DATABASE_URL` set to `synth_voice_worker`.

Rollback: point API and worker back to the owner role; v10 code works the same
because policies do not apply to the owner. To undo the schema use
`synth/server/migrations/0010_rls_per_actor.down.sql`.

Tests: `synth/tests/test_rls_postgres.py` checks isolation, integrations,
revocation, worker grants and migration with a non-superuser owner;
`synth/tests/test_api_rls_walk_postgres.py` walks the API as owner and as
`synth_voice_api` and requires identical responses. Both need
`SYNTH_TEST_DATABASE_URL` pointing to a disposable PostgreSQL.
