#!/usr/bin/env python3
"""Install only Synth's own reversible user-scoped host service."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
LABEL = "dev.synth.voice.pipeline"
target = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
python = ROOT / "synth/.toolchains/build-venv/bin/python"
rtk = shutil.which("rtk")
if not rtk or not python.is_file():
    raise SystemExit("Missing project Python or rtk.")
if target.exists():
    previous = plistlib.loads(target.read_bytes())
    if previous.get("WorkingDirectory") != str(ROOT) or "synth.server.desktop_host" not in previous.get("ProgramArguments", []):
        raise SystemExit("Existing agent belongs to another installation; left unchanged.")
target.parent.mkdir(parents=True, exist_ok=True)
logs = ROOT / "synth/.runtime/logs"
logs.mkdir(parents=True, exist_ok=True, mode=0o700)
configuration = {"Label": LABEL,
    "ProgramArguments": [rtk, "proxy", str(python), "-m", "synth.server.desktop_host"],
    "WorkingDirectory": str(ROOT),
    "EnvironmentVariables": {"PYTHONPATH": str(ROOT), "PATH": str(python.parent) + ":/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin", "SSL_CERT_FILE": "/etc/ssl/cert.pem"},
    # This service handles interactive capture and OAuth callbacks. Background
    # scheduling stalled Python startup on this pilot Mac; avoid inherited IDE paths.
    "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 5, "ProcessType": "Interactive", "Umask": 0o077,
    "StandardOutPath": str(logs / "desktop-host.log"),
    "StandardErrorPath": str(logs / "desktop-host-error.log")}
temporary = target.with_suffix(".plist.part")
temporary.write_bytes(plistlib.dumps(configuration))
temporary.chmod(0o600)
temporary.replace(target)
domain = "gui/" + str(os.getuid())
subprocess.run([rtk, "proxy", "launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
result = subprocess.run([rtk, "proxy", "launchctl", "bootstrap", domain, str(target)], capture_output=True)
if result.returncode:
    raise SystemExit("Host service could not start; configuration preserved for diagnosis.")
print("SYNTH_VOICE_HOST_AGENT_INSTALLED; loopback 18383; reversible user service")
