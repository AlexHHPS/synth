"""Build the arm64 acoustic host using an explicit allowlist of public assets."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import hashlib
import json
import importlib.util

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from synth.worker.asr import MODEL_SHA, MODEL_URL, RUNTIME_MODEL  # noqa: E402
from synth.worker.live_bundle import MANIFEST, describe, data_arguments  # noqa: E402


def transcription_model() -> Path:
    """The pinned whisper model, downloaded once from its public source; the build never reads another app's files."""
    if RUNTIME_MODEL.is_file() and hashlib.sha256(RUNTIME_MODEL.read_bytes()).hexdigest() == MODEL_SHA:
        return RUNTIME_MODEL
    RUNTIME_MODEL.parent.mkdir(parents=True, exist_ok=True)
    partial = RUNTIME_MODEL.with_suffix(".part")
    digest = hashlib.sha256()
    with urllib.request.urlopen(MODEL_URL, timeout=60) as response, partial.open("wb") as out:
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != MODEL_SHA:
        partial.unlink()
        raise SystemExit("Transcription model hash mismatch: " + MODEL_URL)
    partial.replace(RUNTIME_MODEL)
    return RUNTIME_MODEL


OUTPUT = ROOT / "synth/.runtime/distribution"
if importlib.util.find_spec('numpy') is None:
    raise SystemExit('Missing host build dependency: numpy. Install host build requirements before packaging.')
OUTPUT.mkdir(parents=True, exist_ok=True)
digest=hashlib.sha256()
for directory in ['server','worker','speakers','contracts','scripts']:
    for path in sorted((ROOT/'synth'/directory).glob('*.py')):
        digest.update(str(path.relative_to(ROOT)).encode());digest.update(path.read_bytes())
for path in [ROOT/'synth/desktop_entry.py',ROOT/'synth/worker/qwen-runtime.requirements.txt',ROOT/'synth/worker/qwen-model-lock.json',ROOT/'synth/host-build-requirements.txt']:
    digest.update(path.read_bytes())
build_info=OUTPUT/'host-build.json'
build_info.write_text(json.dumps({'id':digest.hexdigest(),'version':json.loads((ROOT/'frontend/package.json').read_text())['version']})+'\n')
command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir",
           "--target-architecture", "arm64", "--name", "synth-voice-host",
           "--distpath", str(OUTPUT / "host"), "--workpath", str(OUTPUT / "build"),
           "--specpath", str(OUTPUT), "--paths", str(ROOT)]
for name in ["uv"]:
    source = shutil.which(name)
    if not source: raise SystemExit("Missing build tool: " + name)
    command += ["--add-binary", source + ":bin"]
assets = OUTPUT / "assets/bin"
assets.mkdir(parents=True, exist_ok=True)
shutil.copy2(ROOT / "frontend/src-tauri/binaries/ffmpeg-aarch64-apple-darwin", assets / "ffmpeg")
command += ["--add-binary", str(assets / "ffmpeg") + ":bin"]
command += ["--add-binary", str(ROOT / "synth/.runtime/swift-build/release/synth-speakers") + ":synth/.runtime/swift-build/release"]
live_manifest = OUTPUT / MANIFEST
live_manifest.write_text(json.dumps(describe(ROOT/'synth/worker'), sort_keys=True)+'\n')
command += data_arguments(ROOT/'synth/worker', live_manifest)
command += ["--add-data", str(ROOT / "synth/scripts/setup-qwen.py") + ":synth/scripts",
            "--add-data", str(ROOT / "synth/speakers/thresholds.json") + ":synth/speakers",
            "--add-data", str(build_info) + ":."]
for source, destination in [
    (ROOT / "synth/.runtime/models/speakers-community1", "synth/.runtime/models/speakers-community1"),
    (ROOT / "synth/.runtime/models/ggml-large-v3-turbo-q5_0.bin", "models"),
    (ROOT / "synth/deployment.json", "synth"),
    (ROOT / "synth/resources/speaker-model-lock.json", "synth/resources"),
    (ROOT / "synth/resources/runtime-policy.json", "synth/resources")]:
    command += ["--add-data", str(source) + ":" + destination]
# numpy is used by the microphone bleed gate (synth/worker/bleed.py).
command += ["--collect-submodules", "numpy"]
command += [str(ROOT / "synth/desktop_entry.py")]
subprocess.run(["rtk", "proxy", *command], cwd=ROOT, check=True)
print("HOST_PACKAGE_BUILT")
