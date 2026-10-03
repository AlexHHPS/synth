"""Assemble the local arm64 pilot; deliberately contains no installation secrets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--format', choices=['zip', 'dmg'], default='zip')
args = parser.parse_args()

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "synth/.runtime/distribution"
OUTPUT.mkdir(parents=True, exist_ok=True)
(ROOT / "synth/.runtime/proof").mkdir(parents=True, exist_ok=True)
BRAND = json.loads((ROOT / "branding.json").read_text())
source = Path(os.environ.get("CARGO_TARGET_DIR", str(ROOT / "synth/.runtime/cargo-target"))) / "release/bundle/macos" / (BRAND["name"] + ".app")
destination = OUTPUT / (BRAND["name"] + ".app")
if destination.exists():
    shutil.rmtree(destination)
shutil.copytree(source, destination)
shutil.copy2(ROOT / "frontend/src-tauri/binaries/ffmpeg-aarch64-apple-darwin", destination / "Contents/MacOS/ffmpeg")
host = destination / "Contents/Resources/synth-host"
shutil.copytree(OUTPUT / "host/synth-voice-host", host)
info = destination / "Contents/Info.plist"
value = plistlib.loads(info.read_bytes()); value["LSMinimumSystemVersion"] = "14.0"; info.write_bytes(plistlib.dumps(value))
notices = destination / "Contents/Resources/Notices"
notices.mkdir()
for source in [ROOT / "LICENSE.md", ROOT / "synth/.toolchains/whisper-portable/LICENSE",
               ROOT / "synth/.runtime/speaker-model-NOTICE.md", ROOT / "synth/.runtime/speaker-model-PROVENANCE.md",
               ROOT / "NOTICE.md", ROOT / "docs/synth/third-party/FluidAudio-LICENSE.txt",
               ROOT / "docs/synth/third-party/Community1-NOTICE.md"]:
    if not source.is_file(): raise FileNotFoundError("Required distribution notice missing: " + source.name)
    if source.is_file():
        shutil.copy2(source, notices / ("whisper-LICENSE" if "whisper-portable" in str(source) else source.name))
for source in [ROOT / "synth/.runtime/ffmpeg-8.0.tar.xz",
               ROOT / "synth/.runtime/ffmpeg-build.json",
               ROOT / "synth/.runtime/ffmpeg-8.0/COPYING.LGPLv2.1"]:
    if not source.is_file(): raise FileNotFoundError("Required FFmpeg source/build/license missing: " + source.name)
    shutil.copy2(source, notices / source.name)
shutil.copy2(ROOT / "synth/.runtime/models/speakers-community1/LICENSE", notices / "Community1-LICENSE.txt")
ffmpeg = OUTPUT / "host/synth-voice-host/_internal/bin/ffmpeg"
expected_binary = ROOT / "frontend/src-tauri/binaries/ffmpeg-aarch64-apple-darwin"
recipe = json.loads((ROOT / "synth/.runtime/ffmpeg-build.json").read_text())
if hashlib.file_digest((ROOT / "synth/.runtime/ffmpeg-8.0.tar.xz").open("rb"), "sha256").hexdigest() != recipe["sha256"]:
    raise ValueError("FFmpeg corresponding source checksum mismatch")
if hashlib.file_digest(expected_binary.open("rb"), "sha256").hexdigest() != recipe["binary_sha256"]:
    raise ValueError("FFmpeg sidecar does not match its build recipe")
if hashlib.file_digest(ffmpeg.open("rb"), "sha256").digest() != hashlib.file_digest(expected_binary.open("rb"), "sha256").digest():
    raise ValueError("Host FFmpeg does not match the prepared sidecar")
with (notices / "ffmpeg-license-and-build.txt").open("w") as stream:
    subprocess.run([str(ffmpeg), "-L"], stdout=stream, stderr=subprocess.STDOUT, check=True)
subprocess.run(["codesign", "--force", "--deep", "--sign", "-",
                "--entitlements", str(ROOT / "frontend/src-tauri/entitlements.plist"), str(destination)], check=True)
subprocess.run(["codesign", "--verify", "--deep", "--strict", str(destination)], check=True)
subprocess.run([str(host / "synth-voice-host"), "--self-check"], check=True)
staging = OUTPUT / "dmg-content"
if staging.exists(): shutil.rmtree(staging)
staging.mkdir(); shutil.copytree(destination, staging / destination.name)
(staging / "Applications").symlink_to("/Applications")
guide = ROOT / "docs/synth/installation.md"
if guide.exists(): shutil.copy2(guide, staging / "Instrucciones.md")
dmg = OUTPUT / (BRAND["name"].replace(" ", "-") + "-arm64." + args.format)
if args.format == 'dmg':
    subprocess.run(["hdiutil", "create", "-ov", "-format", "UDZO", "-volname", BRAND['name'],
                   "-srcfolder", str(staging), str(dmg)], check=True)
else:
    subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', str(staging), str(dmg)], check=True)
digest = hashlib.file_digest(dmg.open("rb"), "sha256").hexdigest()
dmg.with_suffix(dmg.suffix + ".sha256").write_text(digest + "  " + dmg.name + "\n")
(ROOT / "synth/.runtime/proof/portable-package.json").write_text(json.dumps({"status":"BUILT_AND_SELF_CHECKED",
    "architecture":"arm64", "minimum_macos":"14.0", "dmg":str(dmg), "sha256":digest,
    "signature":"ad_hoc", "notarized":False, "second_physical_mac_tested":False}, indent=2))
print("PILOT_PACKAGE_READY", dmg, digest)
