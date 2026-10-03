#!/usr/bin/env python3
"""Install the reversible, user-scoped Synth retention launch agent."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
LABEL = "dev.synth.voice.retention"
target = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
script = ROOT / "synth/scripts/retention.py"
python = ROOT / "synth/.toolchains/build-venv/bin/python"
rtk = shutil.which("rtk")
if not rtk or not python.is_file():
    raise SystemExit("Missing project Python or rtk.")
if target.exists():
    previous = plistlib.loads(target.read_bytes())
    if str(script) not in previous.get("ProgramArguments", []):
        raise SystemExit("Existing launch agent belongs to another installation; left unchanged.")
target.parent.mkdir(parents=True, exist_ok=True)
logs = ROOT / "synth/.runtime/logs"
logs.mkdir(parents=True, exist_ok=True, mode=0o700)
configuration = {"Label": LABEL, "ProgramArguments": [rtk, "proxy", str(python), str(script)],
    "WorkingDirectory": str(ROOT), "EnvironmentVariables": {"PYTHONPATH": str(ROOT)},
    "RunAtLoad": True, "StartInterval": 30, "ProcessType": "Background",
    "StandardOutPath": str(logs / "retention.log"), "StandardErrorPath": str(logs / "retention-error.log")}
target.write_bytes(plistlib.dumps(configuration))
target.chmod(0o600)
domain = "gui/" + str(os.getuid())
subprocess.run([rtk, "proxy", "launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
result = subprocess.run([rtk, "proxy", "launchctl", "bootstrap", domain, str(target)], capture_output=True)
if result.returncode:
    raise SystemExit("Retention launch agent could not start; configuration preserved for diagnosis.")
print("SYNTH_RETENTION_AGENT_INSTALLED; interval 30 seconds; user-scoped")
