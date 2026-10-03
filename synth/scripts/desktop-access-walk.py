#!/usr/bin/env python3
"""Exercise the desktop's real HTTP permissions without exposing bearer tokens."""
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("demo_client", ROOT / "synth/scripts/demo-call.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


def main():
    admin = (ROOT / "synth/.runtime/secrets/bootstrap_admin_key").read_text().strip()
    owner = (Path.home() / "Library/Application Support/dev.synth.voice/desktop-api-key").read_text().strip()
    reader = client.call("POST", "/v1/principals", admin, {"name": "Prueba de acceso desktop · " + str(uuid4())[:8], "kind": "user"})
    key = client.call("POST", "/v1/keys", admin, {"principal_id": reader["id"]})
    token = key["token"]
    folder = None
    checks = []

    def denied(method, path, credential, status, body=None):
        try:
            client.call(method, path, credential, body)
        except ValueError as error:
            assert str(error) == f"api_http_{status}", str(error)
        else:
            raise AssertionError("expected_authorization_denial")

    try:
        folder = client.call("POST", "/v1/folders", owner, {"name": "Prueba de permisos de Synth"})
        assert folder["can_manage"] and folder["can_edit"] and folder["private"]
        access = client.call("GET", f'/v1/folders/{folder["id"]}/members', owner)
        assert access["items"] == []
        assert folder["id"] not in {f["id"] for f in client.call("GET", "/v1/folders", token)["items"]}
        denied("GET", f'/v1/folders/{folder["id"]}/members', token, 404)
        checks.append("private_by_default")
        roster = client.call("GET", "/v1/people", owner)
        assert reader["id"] in {p["id"] for p in roster["items"]}
        checks.append("user_roster")
        client.call("PUT", f'/v1/folders/{folder["id"]}/members', owner, {"principal_id": reader["id"], "role": "reader"})
        view = next(f for f in client.call("GET", "/v1/folders", token)["items"] if f["id"] == folder["id"])
        assert not view["can_manage"] and not view["can_edit"]
        denied("GET", f'/v1/folders/{folder["id"]}/members', token, 403)
        denied("PATCH", f'/v1/folders/{folder["id"]}', token, 404, {"name": "Forbidden"})
        checks.append("reader_cannot_manage_or_edit")
        client.call("PUT", f'/v1/folders/{folder["id"]}/members', owner, {"principal_id": reader["id"], "role": "editor"})
        view = next(f for f in client.call("GET", "/v1/folders", token)["items"] if f["id"] == folder["id"])
        assert not view["can_manage"] and view["can_edit"]
        denied("GET", f'/v1/folders/{folder["id"]}/members', token, 403)
        denied("DELETE", f'/v1/folders/{folder["id"]}/members/{reader["id"]}', token, 403)
        checks.append("editor_cannot_inspect_or_revoke_members")
        access = client.call("GET", f'/v1/folders/{folder["id"]}/members', owner)
        assert access["items"][0]["principal_id"] == reader["id"] and access["items"][0]["role"] == "editor"
        client.call("DELETE", f'/v1/folders/{folder["id"]}/members/{reader["id"]}', owner)
        denied("GET", f'/v1/folders/{folder["id"]}/members', token, 404)
        assert folder["id"] not in {f["id"] for f in client.call("GET", "/v1/folders", token)["items"]}
        checks.append("revocation_immediate")
        result = {"mode": "real_http", "status": "passed", "checks": checks, "human_capture": False}
        client.save(ROOT / "synth/.runtime/proof/desktop-access-walk.json", result)
        print(json.dumps(result))
    finally:
        if folder:
            client.call("DELETE", f'/v1/folders/{folder["id"]}', owner)
        client.call("DELETE", f'/v1/keys/{key["id"]}', admin)


if __name__ == "__main__":
    main()
