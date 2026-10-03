"""Real HTTP concurrency/replay checks using disposable synthetic principals."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from uuid import uuid4

from synth.tests.api_access_walk import call, ADMIN

ROOT = Path(__file__).resolve().parents[2]


def main():
    keys = []
    checks = []
    tag = str(uuid4())
    try:
        tokens = []
        for label in ("A", "B"):
            actor = call("POST", "/v1/principals", ADMIN,
                         {"name": f"Receipt fixture {label} {tag}", "kind": "user"}, 201)
            key = call("POST", "/v1/keys", ADMIN, {"principal_id": actor["id"]}, 201)
            keys.append(key["id"])
            tokens.append(key["token"])
        body = {"title": "Captura sintética", "idempotency_key": "capture:" + tag}
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(lambda _: call("POST", "/v1/meetings", tokens[0], body, 201), range(8)))
        assert len({r["id"] for r in replies}) == 1
        listing = call("GET", "/v1/library", tokens[0])["items"]
        assert len(listing) == 1
        meeting = replies[0]
        checks.append("concurrent_delivery_creates_one_meeting")
        call("POST", "/v1/meetings", tokens[0], {**body, "title": "Cambio"}, 409)
        checks.append("same_receipt_changed_payload_conflicts")
        call("PUT", f'/v1/meetings/{meeting["id"]}/notes', tokens[0],
             {"notes": "Nota sintética persistente", "expected_revision": 1})
        folder = call("POST", "/v1/folders", tokens[0], {"name": "Destino sintético"}, 201)
        call("PUT", f'/v1/meetings/{meeting["id"]}/folder', tokens[0], {"folder_id": folder["id"]})
        replay = call("POST", "/v1/meetings", tokens[0], body, 201)
        assert replay["id"] == meeting["id"] and replay["revision"] == 3
        metadata = call("GET", f'/v1/meetings/{meeting["id"]}', tokens[0])
        assert metadata["notes"] == "Nota sintética persistente" and metadata["folder_id"] == folder["id"]
        checks.append("replay_preserves_edits_and_returns_current_revision")
        other = call("POST", "/v1/meetings", tokens[1], body, 201)
        assert other["id"] != meeting["id"]
        call("GET", f'/v1/meetings/{meeting["id"]}', tokens[1], expected=404)
        checks.append("receipt_is_scoped_to_owner")
        call("POST", "/v1/meetings", tokens[0], {"title": "Sin recibo"}, 201)
        call("POST", "/v1/meetings", tokens[0], {"title": "Sin recibo"}, 201)
        assert len(call("GET", "/v1/library", tokens[0])["items"]) == 3
        checks.append("legacy_creation_remains_available")
    finally:
        for identifier in keys:
            call("DELETE", "/v1/keys/" + identifier, ADMIN)
    report = {"status": "PASS", "mode": "real_http", "checks": checks,
              "data": "synthetic_metadata", "acceptance_rows_closed": []}
    output = ROOT / "synth/.runtime/proof/meeting-receipt-walk.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
