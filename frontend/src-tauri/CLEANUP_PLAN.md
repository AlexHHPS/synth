# Native code maintenance

The compiled application entry point is src/lib.rs. The unused historical copy
lib_old_complex.rs was removed; upstream history remains available in Git.

Synth identity is derived from tauri.conf.json through product_identity.rs.
Do not hardcode an upstream product name in notifications, storage fallbacks or
templates. Preserve immutable SQL migrations when maintaining upgrade compatibility.

See ../../docs/synth/installation.md and ../../docs/synth/community-policy.md.
