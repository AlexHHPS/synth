"""Exercise real HTTP authorization with two users and a scoped integration.

Only synthetic metadata is used. Keys stay in memory and are revoked at the end.
"""
import json
from pathlib import Path
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:18280"
ADMIN = (ROOT / "synth/.runtime/secrets/bootstrap_admin_key").read_text().strip()


def call(method, path, token, body=None, expected=200):
    payload = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=payload, method=method,
                                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        response = urllib.request.urlopen(request, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    assert response.code == expected, (method, path, response.code, expected, response.read().decode())
    return json.loads(response.read())


def main():
    checks = []
    created_keys = []
    tag = str(uuid.uuid4())[:8]
    try:
        alice = call("POST", "/v1/principals", ADMIN, {"name": f"Prueba A {tag}", "kind": "user"}, 201)
        bob = call("POST", "/v1/principals", ADMIN, {"name": f"Prueba B {tag}", "kind": "user"}, 201)
        machine = call("POST", "/v1/principals", ADMIN, {"name": f"Integración {tag}", "kind": "machine"}, 201)
        ak = call("POST", "/v1/keys", ADMIN, {"principal_id": alice["id"]}, 201); created_keys.append(ak["id"])
        bk = call("POST", "/v1/keys", ADMIN, {"principal_id": bob["id"]}, 201); created_keys.append(bk["id"])
        folder = call("POST", "/v1/folders", ak["token"], {"name": f"Compartida {tag}"}, 201)
        private = call("POST", "/v1/folders", ak["token"], {"name": f"Privada {tag}"}, 201)
        meeting = call("POST", "/v1/meetings", ak["token"], {"title": "Reunión sintética", "folder_id": folder["id"]}, 201)
        hidden = call("POST", "/v1/meetings", ak["token"], {"title": "Oculta", "folder_id": private["id"]}, 201)
        loose = call("POST", "/v1/meetings", ak["token"], {"title": "Sin carpeta"}, 201)
        call("GET", f'/v1/meetings/{meeting["id"]}', bk["token"], expected=404)
        call("GET", f'/v1/meetings/{loose["id"]}', bk["token"], expected=404)
        assert call("GET", "/v1/folders", bk["token"])["items"] == []
        checks.extend(["private_default", "direct_id_denied", "local_user_auth"])
        call("PUT", f'/v1/folders/{folder["id"]}/members', ak["token"], {"principal_id": bob["id"], "role": "reader"})
        assert call("GET", f'/v1/meetings/{meeting["id"]}', bk["token"])["title"] == "Reunión sintética"
        call("PUT", f'/v1/meetings/{meeting["id"]}/notes', bk["token"], {"notes": "No autorizado", "expected_revision": 1}, 404)
        checks.append("explicit_share")
        mk = call("POST", "/v1/keys", ak["token"], {"principal_id": machine["id"], "folder_ids": [folder["id"]]}, 201); created_keys.append(mk["id"])
        call("GET", f'/v1/meetings/{meeting["id"]}', mk["token"])
        call("GET", f'/v1/meetings/{hidden["id"]}', mk["token"], expected=404)
        call("GET", f'/v1/meetings/{loose["id"]}', mk["token"], expected=404)
        call("POST", "/v1/folders", mk["token"], {"name": "No"}, 403)
        assert [f["id"] for f in call("GET", "/v1/folders", mk["token"])["items"]] == [folder["id"]]
        checks.append("scoped_machine_key")
        assert call("PUT", f'/v1/meetings/{meeting["id"]}/notes', ak["token"], {"notes": "Nota humana sintética", "expected_revision": 1})["revision"] == 2
        call("PUT", f'/v1/meetings/{meeting["id"]}/notes', ak["token"], {"notes": "Edición antigua", "expected_revision": 1}, 409)
        assert call("GET", f'/v1/meetings/{meeting["id"]}', ak["token"])["notes"] == "Nota humana sintética"
        checks.extend(["notes_edit", "optimistic_edit_conflict"])
        call("DELETE", f'/v1/folders/{folder["id"]}/members/{bob["id"]}', ak["token"])
        call("GET", f'/v1/meetings/{meeting["id"]}', bk["token"], expected=404)
        checks.append("revoked_membership")
        call("DELETE", f'/v1/keys/{mk["id"]}', ADMIN)
        call("GET", f'/v1/meetings/{meeting["id"]}', mk["token"], expected=401)
        checks.append("revoked_key")
    finally:
        for key in created_keys:
            call("DELETE", f"/v1/keys/{key}", ADMIN)
    report = {"mode": "real", "transport": "HTTP", "status": "PASS", "checks": checks}
    (ROOT / "synth/.runtime/api-access-walk.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
