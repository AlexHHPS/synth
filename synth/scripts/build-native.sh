#!/bin/sh
# Build only: installation and human capture checks are separate, observable steps.
set -eu
SYNTH_ENV_SCRIPT="$(dirname "$0")/toolchain-env.sh"
export SYNTH_ENV_SCRIPT
. "$SYNTH_ENV_SCRIPT"
cd "$SYNTH_ROOT"

export DEVELOPER_DIR="${SYNTH_XCODE_DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
if [ ! -x "$DEVELOPER_DIR/usr/bin/xcodebuild" ]; then
    echo "Full Xcode is required at DEVELOPER_DIR for ScreenCaptureKit and Swift builds." >&2
    exit 1
fi
if [ ! -f synth/.runtime/ffmpeg-build.json ]; then
    echo "Run synth/scripts/prepare-ffmpeg.py before the native build." >&2
    exit 1
fi
rtk proxy "$DEVELOPER_DIR/usr/bin/xcodebuild" -checkFirstLaunchStatus
rtk proxy xcrun swift build --package-path synth/speakers/native \
    --scratch-path synth/.runtime/swift-build \
    --cache-path synth/.toolchains/swift-cache \
    --config-path synth/.toolchains/swift-config \
    --security-path synth/.toolchains/swift-security -c release
rtk proxy cargo build -p llama-helper --release --features metal
rtk proxy cp "$CARGO_TARGET_DIR/release/llama-helper" \
    frontend/src-tauri/binaries/llama-helper-aarch64-apple-darwin
cd frontend
export NEXT_TELEMETRY_DISABLED=1
rtk proxy ./node_modules/.bin/tauri build --bundles app
