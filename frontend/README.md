# Synth desktop

The native Apple Silicon client for Synth, built with Tauri, Rust, Next.js and React.
Capture, live transcription, local acoustic processing and voice enrollment run on
the Mac. Library text, access control and document jobs use the operator's central API.

Start with [build and installation](../docs/synth/installation.md),
[white label configuration](../docs/synth/white-label.md),
[architecture](../docs/synth/architecture.md) and [REST/MCP](../docs/synth/integrations.md).

## Development

Use Node 22 and pnpm 10.17.1. Install the frozen lockfile before starting the UI:

```sh
pnpm install --frozen-lockfile
pnpm dev
pnpm exec tsc --noEmit
pnpm build
```

A browser preview cannot capture Mac audio or access the native credential bridge.
Use the complete source build guide for native capabilities, the bundled acoustic
host and the configured central backend. The window starts in the Synth workspace.

`src/synth` owns the active workspace and public design system.
`src/components` includes shared controls and inherited desktop screens.
`src-tauri` owns capture, permissions, live ASR and the approved destination bridge.
Other platform code is inherited reference material; the complete pipeline is
currently validated only on Apple Silicon/macOS 14+.

The repository retains the Meetily Community MIT license and third-party notices.
See [edition and attribution policy](../docs/synth/community-policy.md).
