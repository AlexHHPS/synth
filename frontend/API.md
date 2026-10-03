# Synth integration surfaces

The public API is the central authenticated library, not the archived whisper-server.
Use [REST and MCP](../docs/synth/integrations.md) for endpoints, scopes and client setup.
The Mac acoustic host exposes a loopback control bridge for the native application;
it is not the integration endpoint for other products.

The UI uses Tauri commands for native capture and the credential bridge. The older
Meetily whisper-server protocol remains under `backend/` for historical research.
It is not deployed or packaged as part of Synth.
