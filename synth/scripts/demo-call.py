#!/usr/bin/env python3
"""Process a recorded call through real local ASR/diarization and the queued API."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from synth.contracts.transcript import Transcript
from synth.speakers.native import infer, attach_speaker_labels
from synth.worker.asr import transcribe, normalize_audio
from synth.worker.journal import Journal, file_hash

BASE = "http://127.0.0.1:18280"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def call(method, path, token, body=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise ValueError("api_http_" + str(error.code)) from None
    except (urllib.error.URLError, TimeoutError):
        raise ValueError("api_unavailable") from None


def save(path, value):
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_suffix(path.suffix + ".part")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description="Acta real de una llamada; audio siempre local.")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--title", default="Demo CTO · Google Meet")
    parser.add_argument("--notes-file", type=Path)
    parser.add_argument("--folder-id")
    parser.add_argument("--synthetic", action="store_true", help="Solo para un fixture artificial.")
    parser.add_argument("--consent-confirmed", action="store_true", help="Los participantes saben y aceptan la grabación.")
    args = parser.parse_args()
    if not args.synthetic and not args.consent_confirmed:
        parser.error("Confirma el consentimiento de los participantes con --consent-confirmed.")
    if not args.audio.is_file():
        parser.error("No se encuentra el audio de la llamada.")
    policy = json.loads((ROOT / "synth/resources/runtime-policy.json").read_text())
    if policy.get("id") != "operator-configured-gateway-v1":
        raise ValueError("runtime_policy_missing")
    token = (ROOT / "synth/.runtime/secrets/bootstrap_admin_key").read_text().strip()
    if call("GET", "/health", token).get("status") != "ready":
        raise ValueError("api_not_ready")

    run_id = str(uuid4())
    output = ROOT / "synth/.runtime/demo" / run_id
    output.mkdir(parents=True, mode=0o700)
    journal = Journal(ROOT / "synth/.runtime/capture-journal")
    journal.cleanup()
    local_meeting, sources = journal.start(["import"])
    temporary = journal.raw / local_meeting / "normalized.wav"
    temporary.parent.mkdir(parents=True, mode=0o700)
    journal.track_temporary(local_meeting, temporary)
    try:
        print("1/4 · Preparando audio local…", flush=True)
        normalize_audio(args.audio, temporary)
        journal.append(sources["import"], 0, 0, temporary)
        journal.seal_source(sources["import"], 1)
        journal.seal(local_meeting)
        print("2/4 · Transcribiendo con Whisper local…", flush=True)
        transcript, raw_asr = transcribe(temporary, output)
        journal.mark_processed(local_meeting, "asr")
        print("3/4 · Separando hablantes con Core ML local…", flush=True)
        diarization = infer("diarize", temporary, temporary.parent)
        transcript = Transcript.model_validate(attach_speaker_labels(transcript, diarization)).model_dump()
        # Enrollment/calibration is a separate acceptance requirement. Never invent employee names.
        identity = {"state": "unavailable", "reason": "employee_voice_calibration_pending",
                    "automatic_employee_assignments": 0}
        journal.mark_processed(local_meeting, "identity")
        save(output / "transcript.json", transcript)
        save(output / "diarization.json", diarization)
        save(output / "identity-status.json", identity)
    finally:
        journal.cleanup()
        journal.close()

    folder = args.folder_id or call("POST", "/v1/folders", token, {"name": "Demo CTO · Google Meet"})["id"]
    meeting = call("POST", "/v1/meetings", token, {"title": args.title, "folder_id": folder})
    if args.notes_file:
        call("PUT", f'/v1/meetings/{meeting["id"]}/notes', token,
             {"notes": args.notes_file.read_text(), "expected_revision": meeting["revision"]})
    transcript_hash = hashlib.sha256(json.dumps(transcript, sort_keys=True).encode()).hexdigest()
    job = call("POST", f'/v1/meetings/{meeting["id"]}/transcript', token,
               {"transcript": transcript, "idempotency_key": "demo:" + transcript_hash})
    save(output / "run.json", {"run_id": run_id, "data": "synthetic" if args.synthetic else "real_call",
         "source_audio_sha256": file_hash(args.audio), "meeting_id": meeting["id"], "folder_id": folder,
         "job_id": job["job_id"], "runtime_policy_id": policy["id"], "identity": identity,
         "original_input_preserved": True, "managed_raw_audio_deleted": not temporary.exists()})
    print("4/4 · Generando acta con OmniRoute local-combo…", flush=True)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        state = call("GET", f'/v1/jobs/{job["job_id"]}', token)
        if state["state"] == "succeeded":
            document = call("GET", f'/v1/meetings/{meeting["id"]}/document', token)
            save(output / "acta.json", document)
            save(output / "acta.md", document["markdown"])
            print("Acta guardada: " + str(output / "acta.md"))
            print("Identidad: hablantes anónimos; calibración de empleados pendiente.")
            return
        if state["state"] in {"failed", "cancelled"}:
            raise ValueError("document_job_" + state["state"] + ":" + str(state.get("error_code", "unknown")))
        time.sleep(1)
    raise ValueError("document_wait_timeout_job_retained")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print("Proceso detenido: " + str(error), file=sys.stderr)
        sys.exit(1)
