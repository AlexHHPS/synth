"""Build the arm64 acoustic host using an explicit allowlist of public assets."""
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "synth/.runtime/distribution"
OUTPUT.mkdir(parents=True, exist_ok=True)
command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir",
           "--target-architecture", "arm64", "--name", "synth-voice-host",
           "--distpath", str(OUTPUT / "host"), "--workpath", str(OUTPUT / "build"),
           "--specpath", str(OUTPUT), "--paths", str(ROOT)]
assets = OUTPUT / "assets/bin"
assets.mkdir(parents=True, exist_ok=True)
shutil.copy2(ROOT / "frontend/src-tauri/binaries/ffmpeg-aarch64-apple-darwin", assets / "ffmpeg")
command += ["--add-binary", str(assets / "ffmpeg") + ":bin"]
command += ["--add-binary", str(ROOT / "synth/.runtime/whisper-portable-build/bin/whisper-cli") + ":bin"]
command += ["--add-binary", str(ROOT / "synth/.runtime/swift-build/release/synth-speakers") + ":synth/.runtime/swift-build/release"]
for source, destination in [
    (ROOT / "synth/.runtime/models/speakers-community1", "synth/.runtime/models/speakers-community1"),
    (ROOT / "synth/.runtime/models/ggml-large-v3-turbo-q5_0.bin", "models"),
    (ROOT / "synth/deployment.json", "synth"),
    (ROOT / "synth/resources/speaker-model-lock.json", "synth/resources"),
    (ROOT / "synth/resources/runtime-policy.json", "synth/resources")]:
    command += ["--add-data", str(source) + ":" + destination]
command += [str(ROOT / "synth/desktop_entry.py")]
subprocess.run(["rtk", "proxy", *command], cwd=ROOT, check=True)
print("HOST_PACKAGE_BUILT")
