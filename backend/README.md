# Archived upstream backend

This directory retains the old Meetily Python/FastAPI and whisper-server implementation
for historical reference. Its scripts can reference old product paths and upstream
downloads. Do not run them against Synth or another installed app.

Synth uses the independent service in `synth/server`, worker in `synth/worker` and
deployment configuration in `synth/deploy`.
See [architecture](../docs/synth/architecture.md), [installation](../docs/synth/installation.md)
and [REST/MCP](../docs/synth/integrations.md).
The archive is not included in Synth's container image or acoustic package.
