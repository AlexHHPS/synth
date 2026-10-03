"""Create project-owned secrets exclusively, with no values printed."""
import base64
import os
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[2] / "synth/.runtime/secrets"
root.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(root, 0o700)
for name in ("postgres_password", "bootstrap_admin_key", "profile_encryption_key", "voice_profile_encryption_key"):
    path = root / name
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"{name}: present; preserved")
        continue
    with os.fdopen(handle, "w") as stream:
        stream.write(base64.b64encode(secrets.token_bytes(32)).decode() if name == "voice_profile_encryption_key" else secrets.token_hex(32))
    print(f"{name}: generated")
