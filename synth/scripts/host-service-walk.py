#!/usr/bin/env python3
"""Verify the real loopback service with a previously processed synthetic task."""
import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from synth.worker.desktop_pipeline import APPDATA, CAPTURE, STATE
from synth.worker.desktop_queue import DesktopQueue


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    token = (APPDATA / "desktop-api-key").read_text().strip()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(method, path, body=None, expected=200, authorized=True, origin=None):
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer " + token
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request("http://127.0.0.1:18383" + path, method=method,
            headers=headers, data=json.dumps(body).encode() if body is not None else None)
        try:
            response = opener.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        assert response.code == expected, (method, path, response.code, expected)
        return json.loads(response.read())

    checks = []
    call("GET", "/v1/status", authorized=False, expected=401)
    call("GET", "/v1/status", origin="https://example.invalid", expected=403)
    checks.extend(["authentication_required", "foreign_origin_denied"])
    status = call("GET", "/v1/status")
    assert status["audio_location"] == "this_mac"
    task = call("GET", "/v1/tasks/" + args.task_id)
    assert task["state"] == "succeeded" and task["meeting_id"] and task["job_id"]
    assert not {"metadata", "sources", "checkpoints", "result", "lease_token"}.intersection(task)
    assert task["identity"]["state"] == "unavailable"
    checks.extend(["actual_completed_host_task_visible", "audio_paths_and_lease_tokens_not_exported", "identity_pending_is_explicit"])
    queue = DesktopQueue(STATE / "queue.sqlite3", CAPTURE)
    try:
        private = queue.get(args.task_id)
    finally:
        queue.close()
    metadata = private["metadata"]
    body = {"sources": {k: v["path"] for k, v in metadata["sources"].items()},
            "title": metadata["title"], "folder_id": metadata["folder_id"], "consent_confirmed": True}
    replay = call("POST", "/v1/captures", body, expected=201)
    assert replay["id"] == task["id"]
    checks.append("duplicate_capture_recovers_same_task")
    call("POST", "/v1/captures", {**body, "consent_confirmed": False}, expected=409)
    call("POST", "/v1/captures", {**body, "sources": {"import": "/etc/hosts"}}, expected=409)
    call("GET", "/v1/tasks/not-a-uuid", expected=400)
    checks.extend(["capture_requires_consent", "outside_audio_path_denied", "malformed_task_denied"])
    report = {"status": "PASS", "mode": "real_loopback_http", "checks": checks,
              "task_id": task["id"], "meeting_id": task["meeting_id"], "job_id": task["job_id"],
              "data": "synthetic_audio", "native_capture_proven": False, "acceptance_rows_closed": []}
    (ROOT / "synth/.runtime/proof/host-service-walk.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
