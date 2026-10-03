#!/bin/sh
# Source without changing global Rust/Python installations.
SYNTH_ROOT=$(CDPATH= cd -- "$(dirname -- "${SYNTH_ENV_SCRIPT:-synth/scripts/toolchain-env.sh}")/../.." && pwd)
export SYNTH_ROOT
export PATH="$SYNTH_ROOT/synth/.toolchains/build-venv/bin:$PATH"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$SYNTH_ROOT/synth/.runtime/cargo-target}"
export SSL_CERT_FILE=/etc/ssl/cert.pem
