# Community source, white label identity and redistribution

Synth is an independent derivative of Meetily Community Edition. The source
license retained from that edition is [MIT](../../LICENSE.md), copyright
2024 Zackriya Solutions. The upstream [source license](https://github.com/Zackriya-Solutions/meetily/blob/main/LICENSE.md)
permits use, modification and distribution, including commercial use, provided
the copyright and permission notice accompany copies or substantial portions.

## What Synth preserves

- The complete, unchanged upstream MIT license and copyright in LICENSE.md.
- A concise origin attribution in About, README and NOTICE.md.
- Notices and licenses for dependencies and the exact models distributed.
- Original copyright headers and migration history needed for compatibility.

The product identity, visual assets, links and build identifier are configured
independently. Attribution describes origin; it is not an endorsement or an
official Meetily edition. Synth uses its own visual assets rather than marketing
screenshots or logos from Meetily. MIT is a code license; this policy does not
claim a separate trademark license.

## Community and commercial editions

Meetily publishes [Community and Pro editions](https://meetily.ai/downloads),
and identifies Pro as a different codebase in its [source README](https://github.com/Zackriya-Solutions/meetily).
Synth derives from the MIT Community source. Its central library, REST/MCP,
voice catalog and local speaker pipeline are implemented in this repository
using their documented dependencies. Similar functionality does not mean that
Synth includes Meetily Pro or grants access to its licensed services, support,
trial, Automation API or subscription.

Do not import proprietary Pro code, license keys or service credentials.
Historical migration names mentioning licensing remain unchanged to preserve
database migration checksums; they do not enable a Pro service or subscription.
The old backend directory is explicitly archived and excluded from packaging.

## Dependency licenses

FluidAudio is Apache-2.0, as reproduced in the checked-in
[license](third-party/FluidAudio-LICENSE.txt). Selected Community-1 artifacts use
CC-BY-4.0 under the scope and attribution in the
[model notice](third-party/Community1-NOTICE.md); this does not cover unrelated
legacy models. Whisper/whisper.cpp retain their upstream MIT terms.
The source-built Apple Silicon FFmpeg executable uses LGPL-2.1-or-later; its
corresponding source archive, build flags and license accompany the package.
See [NOTICE.md](../../NOTICE.md) and the exact runtime's notices before distributing.

## Distribution checklist

Keep LICENSE.md and NOTICE.md with source distributions and preserve the notices
assembled by package-pilot.py for binary distributions. Publish only reviewed,
audited source and artifacts, with their checksums. Configure product links to
the distributor's documentation and support, while retaining upstream attribution.
Do not advertise an official relationship, a Pro entitlement, fully offline
summaries, validated voice identification or platform support that this build
does not provide. Signing/notarization and independent voice calibration remain
separate release requirements.
