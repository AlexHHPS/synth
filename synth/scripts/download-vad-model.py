#!/usr/bin/env python3
"""Fetch the pinned Silero VAD model for whisper.cpp (ggml-org/whisper-vad, MIT) into the runtime."""
import hashlib
import ssl
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from synth.worker.asr import VAD_MODEL, VAD_SHA  # noqa: E402

URL = "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin"

if VAD_MODEL.is_file() and hashlib.sha256(VAD_MODEL.read_bytes()).hexdigest() == VAD_SHA:
    print("VAD_MODEL_PRESENT")
    sys.exit(0)
context = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
data = urllib.request.urlopen(URL, context=context, timeout=60).read()
if hashlib.sha256(data).hexdigest() != VAD_SHA:
    raise SystemExit("VAD model hash mismatch")
VAD_MODEL.parent.mkdir(parents=True, exist_ok=True)
VAD_MODEL.write_bytes(data)
print("VAD_MODEL_DOWNLOADED")
