"""Publish an explicitly selected recovered transcript as a new meeting version.

Uses the desktop's normal authentication and API ACLs. Never updates a database
directly, replaces old versions, or uploads audio. Defaults to validation only.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from synth.contracts.transcript import Transcript
from synth.worker.desktop_pipeline import LocalAPI


def recover(api, meeting_id, expected_title, transcript, apply=False):
    meeting_id = str(UUID(meeting_id))
    content = Transcript.model_validate(transcript).model_dump()
    actor = api.call("GET", "/v1/me")
    if actor["kind"] != "user":
        raise ValueError("recovery_requires_user_session")
    meeting = api.call("GET", "/v1/meetings/" + meeting_id)
    if meeting["title"] != expected_title:
        raise ValueError("recovery_meeting_title_changed")
    digest = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    result = {"meeting_id": meeting_id, "segments": len(content["segments"]),
              "sha256": digest, "applied": False}
    if apply:
        receipt = api.call("POST", f"/v1/meetings/{meeting_id}/transcript", {
            "transcript": content, "idempotency_key": "recovery:" + digest})
        result.update(applied=True, **receipt)
        job = api.call("GET", "/v1/jobs/" + receipt["job_id"])
        result["job_state"] = job["state"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meeting-id", required=True)
    parser.add_argument("--expected-title", required=True)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Create a new transcript version and document job")
    args = parser.parse_args()
    try:
        result = recover(LocalAPI(), args.meeting_id, args.expected_title,
                         json.loads(args.transcript.read_text()), args.apply)
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, KeyError):
        # Never print tokens, real transcript contents or raw HTTP responses.
        print("Recovery did not complete. Check the signed-in account, meeting title and backend connection.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
