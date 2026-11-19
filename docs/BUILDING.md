# Building Synth

Use the [complete Apple Silicon build guide](synth/installation.md). It includes
branding, runtime models, the standalone acoustic host, native client and packaging.
A plain Tauri build alone does not produce a complete distributable Synth product.

Windows/Linux paths remain inherited development references; a full Synth release
for these platforms is not currently validated.

## Desktop diagnostics

Development builds log at Debug level. Release builds log at Info level.
Start the release executable with `SYNTH_DEBUG_LOG=1` for more detail.
Logs are written to `logs-dev/` or `logs/` in the application data directory.
The macOS acoustic host uses its own `host.log` and `host-error.log` files.
