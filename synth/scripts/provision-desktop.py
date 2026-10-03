#!/usr/bin/env python3
"""Provision one local desktop identity; bearer material never appears in output."""
import argparse
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("demo_client", ROOT / "synth/scripts/demo-call.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


def private_write(path, text):
    temporary = path.with_suffix(".part")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    args = parser.parse_args()
    directory = Path.home() / "Library/Application Support/dev.synth.voice"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_file, metadata = directory / "desktop-api-key", directory / "desktop-identity.json"
    admin = (ROOT / "synth/.runtime/secrets/bootstrap_admin_key").read_text().strip()
    actor = None
    if key_file.exists():
        try:
            actor = client.call("GET", "/v1/me", key_file.read_text().strip())
        except ValueError as error:
            if str(error) != 'api_http_401': raise
    if actor is None:
        actor = client.call("POST", "/v1/principals", admin,
                            {"name": "Operador local · Synth", "kind": "user"})
        key = client.call("POST", "/v1/keys", admin, {"principal_id": actor["id"]})
        private_write(key_file, key["token"] + "\n")
        private_write(metadata, json.dumps({"principal_id": actor["id"], "key_id": key["id"]}) + "\n")
    if actor["id"] == "00000000-0000-4000-8000-000000000001" or actor["kind"] != "user":
        raise ValueError("desktop_identity_must_be_nonadmin_user")
    print("DESKTOP_IDENTITY_READY; dedicated user; credential file mode 0600")


if __name__ == "__main__":
    main()
