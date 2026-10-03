"""Actual HTTP integration issuance, scope boundaries and issuer revocation."""
import json
from pathlib import Path
from uuid import uuid4
import urllib.request
import urllib.error
from synth.tests.api_access_walk import ADMIN

ROOT = Path(__file__).resolve().parents[2]


def call(method, path, token, body=None, expected=200):
    request = urllib.request.Request("http://127.0.0.1:18280" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        response = urllib.request.urlopen(request, timeout=20)
    except urllib.error.HTTPError as error:
        response = error
    # Issuance responses contain one-time credentials: never print response bodies.
    assert response.code == expected, (method, path, response.code, expected)
    return json.loads(response.read())


def main():
    keys, integrations, checks = [], [], []
    tag = str(uuid4())
    try:
        tokens = []
        people = []
        for label in ("Owner", "Other"):
            person = call("POST", "/v1/principals", ADMIN, {"name": f"Integration fixture {label} {tag}", "kind": "user"}, 201)
            people.append(person)
            key = call("POST", "/v1/keys", ADMIN, {"principal_id": person["id"]}, 201)
            keys.append(key["id"]); tokens.append(key["token"])
        owner, other = tokens
        first = call("POST", "/v1/folders", owner, {"name": "Allowed synthetic folder"}, 201)
        hidden = call("POST", "/v1/folders", owner, {"name": "Private synthetic folder"}, 201)
        foreign = call("POST", "/v1/folders", other, {"name": "Other owner synthetic folder"}, 201)
        meeting = call("POST", "/v1/meetings", owner, {"title": "Synthetic integration meeting", "folder_id": first["id"]}, 201)
        hidden_meeting = call("POST", "/v1/meetings", owner, {"title": "Private synthetic meeting", "folder_id": hidden["id"]}, 201)
        loose = call("POST", "/v1/meetings", owner, {"title": "No-folder synthetic meeting"}, 201)
        integration = call("POST", "/v1/integrations", owner, {"name": "Fixture consumer", "folder_ids": [first["id"], first["id"]]}, 201)
        integrations.append((owner, integration["id"]))
        assert integration["read_only"] and integration["folder_ids"] == [first["id"]]
        call("GET", f'/v1/meetings/{meeting["id"]}', integration["token"])
        call("GET", f'/v1/meetings/{hidden_meeting["id"]}', integration["token"], expected=404)
        call("GET", f'/v1/meetings/{loose["id"]}', integration["token"], expected=404)
        call("PUT", f'/v1/meetings/{meeting["id"]}/notes', integration["token"], {"notes": "Denied", "expected_revision": 1}, 403)
        call("GET", "/v1/integrations", integration["token"], expected=403)
        checks.extend(["scope_deduplicated", "explicit_folder_read", "other_folder_and_private_meetings_denied", "machine_write_and_key_management_denied"])
        listed = call("GET", "/v1/integrations", owner)["items"]
        assert len(listed) == 1 and "token" not in listed[0] and "token_hash" not in listed[0]
        assert call("GET", "/v1/integrations", other)["items"] == []
        call("DELETE", f'/v1/integrations/{integration["id"]}', other, expected=404)
        checks.extend(["list_never_recovers_token", "other_issuer_cannot_list_or_revoke"])
        call("POST", "/v1/integrations", owner, {"name": "No scopes", "folder_ids": []}, 422)
        call("POST", "/v1/integrations", owner, {"name": "Wrong owner", "folder_ids": [foreign["id"]]}, 404)
        call("PUT", f'/v1/folders/{first["id"]}/members', owner, {"principal_id": people[1]["id"], "role": "editor"})
        call("POST", "/v1/integrations", other, {"name": "Editor cannot issue", "folder_ids": [first["id"]]}, 403)
        checks.extend(["empty_and_foreign_scopes_rejected", "shared_editor_cannot_issue"])
        call("DELETE", f'/v1/integrations/{integration["id"]}', owner)
        call("GET", f'/v1/meetings/{meeting["id"]}', integration["token"], expected=401)
        call("DELETE", f'/v1/integrations/{integration["id"]}', owner)
        checks.append("issuer_revokes_immediately_and_idempotently")
        # The older issuance API must record the issuer too.
        machine = call("POST", "/v1/principals", ADMIN, {"name": "Legacy fixture consumer", "kind": "machine"}, 201)
        legacy = call("POST", "/v1/keys", owner, {"principal_id": machine["id"], "folder_ids": [first["id"]]}, 201)
        keys.append(legacy["id"])
        call("DELETE", f'/v1/keys/{legacy["id"]}', other, expected=403)
        call("DELETE", f'/v1/keys/{legacy["id"]}', owner)
        call("GET", f'/v1/meetings/{meeting["id"]}', legacy["token"], expected=401)
        checks.append("legacy_key_issuer_can_revoke_without_admin")
    finally:
        for token, identifier in integrations:
            call("DELETE", "/v1/integrations/" + identifier, token)
        for identifier in keys:
            call("DELETE", "/v1/keys/" + identifier, ADMIN)
    report = {"mode": "real_http", "status": "PASS", "data": "synthetic_metadata",
              "checks": checks, "acceptance_rows_closed": []}
    (ROOT / "synth/.runtime/proof/integration-walk.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
