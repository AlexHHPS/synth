"""Host acoustic worker. Docker owns text/documents; this process owns audio.

One OS lock covers the whole acoustic worker lifetime. Queue leases fence
checkpoints; idempotent API receipts fence remote writes after host crashes.
No employee identity is asserted before independent calibration is available.
"""
from contextlib import contextmanager
import fcntl
import json
import os
import ssl
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

from synth.contracts.transcript import Transcript
from synth.speakers.native import infer, attach_speaker_labels
from .asr import normalize_audio, transcribe
from .desktop_queue import DesktopQueue, QueueError
from .journal import file_hash

ROOT = Path(__file__).resolve().parents[2]
APPDATA = Path.home() / "Library/Application Support/dev.synth.voice"
CAPTURE = APPDATA / "capture/raw"
STATE = APPDATA / "pipeline"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class LocalAPI:
    def __init__(self, credential=APPDATA / "desktop-api-key"):
        self.credential = Path(credential)
        from synth.config import API_URL, AUTH_MODE
        configuration = APPDATA / "backend-config.json"
        self.base_url = json.loads(configuration.read_text()).get("api_url") if configuration.exists() else API_URL
        from synth.config import API_URL
        if self.base_url != API_URL:
            raise ValueError("unapproved_product_backend")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile="/etc/ssl/cert.pem")))
        self.auth_mode = json.loads(configuration.read_text()).get("auth_mode", "keys") if configuration.exists() else AUTH_MODE
        self.expected_principal = None

    def call(self, method, path, body=None):
        if self.auth_mode == "supabase":
            from synth.server.desktop_auth import access
            session = access()
            if self.expected_principal is not None and self.expected_principal != session["principal_id"]:
                raise ValueError("auth_identity_changed")
            self.expected_principal = session["principal_id"]
            token = session["access_token"]
        else:
            token = self.credential.read_text().strip()
        request = urllib.request.Request(self.base_url + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=20) as response:
                content = response.read(16 * 1024 * 1024 + 1)
                if len(content) > 16 * 1024 * 1024:
                    raise ValueError("api_response_too_large")
                return json.loads(content)
        except urllib.error.HTTPError as error:
            raise ValueError("api_http_" + str(error.code)) from None
        except (urllib.error.URLError, TimeoutError):
            raise ValueError("api_unavailable") from None


def save_json(path, content):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".part")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(content, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {"path": str(path), "sha256": file_hash(path)}


def restore_json(descriptor, root):
    path = Path(descriptor["path"]).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or file_hash(path) != descriptor["sha256"]:
        raise ValueError("checkpoint_file_invalid")
    return json.loads(path.read_text())


def delete_processed_audio(task, capture):
    """Erase only this closed capture and its normalized copies, before LLM work."""
    root = Path(capture).resolve(strict=True)
    paths = [Path(d["path"]) for d in task["metadata"]["sources"].values()]
    normalized = root / ("processed-" + task["id"])
    if normalized.exists() and not normalized.is_symlink():
        paths.extend(p for p in normalized.rglob("*") if p.is_file())
    for descriptor in task["metadata"]["sources"].values():
        source = Path(descriptor["path"])
        receipt = source.parent / "capture.json"
        if source.parent.name == "sources" and receipt.is_file() and not receipt.is_symlink():
            data = json.loads(receipt.read_text())
            if data.get("state") == "closed" and data.get("capture_id") == task["metadata"].get("capture_id"):
                paths.extend(p for p in source.parent.parent.rglob("*") if p.is_file())
    removed = 0
    for path in set(paths):
        if path.suffix.lower() not in {".wav", ".mp4", ".m4a", ".flac", ".mp3", ".ogg", ".opus", ".webm", ".aac", ".aiff", ".aif"}:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("audio_cleanup_path_denied")
        if path.exists():
            path.unlink()
            removed += 1
    return removed


@contextmanager
def inference_lock(state):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(state / "acoustic-worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("acoustic_worker_already_running") from None
        yield fd
    finally:
        os.close(fd)


class Lease:
    def __init__(self, database, capture, task):
        self.database, self.capture, self.task = database, capture, task
        self.stop = threading.Event()
        self.failure = None

    def __enter__(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def run(self):
        queue = None
        try:
            queue = DesktopQueue(self.database, self.capture)
            while not self.stop.wait(5):
                if not queue.heartbeat(self.task["id"], self.task["lease_token"], lease_seconds=60):
                    self.failure = "task_cancel_requested"
                    return
        except Exception:
            self.failure = "lease_heartbeat_failed"
        finally:
            if queue is not None:
                queue.close()

    def check(self, queue):
        if self.failure:
            raise QueueError(self.failure)
        if not queue.heartbeat(self.task["id"], self.task["lease_token"], lease_seconds=60):
            raise QueueError("task_cancel_requested")

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=15)


def canonical_transcript(transcripts, diarizations):
    """Preserve source provenance; channel identity is never employee identity."""
    segments = []
    duration = 0
    fingerprints = []
    has_voice = any(d.get("segments") for d in diarizations.values())
    for source, transcript in transcripts.items():
        duration = max(duration, transcript["duration_ms"])
        fingerprints.append(transcript["model_fingerprint"])
        # Once another source contains usable speech, an empty acoustic source
        # contributes no speakers or ASR hallucinations to the meeting. Raw ASR
        # remains in its checkpoint. Very short notes may produce no embedding
        # on any source: retain their ASR with unknown labels rather than dropping it.
        if has_voice and diarizations[source].get("speech_state") == "no_usable_speech":
            continue
        labelled = attach_speaker_labels(transcript, diarizations[source])
        for segment in labelled["segments"]:
            segment["source_id"] = source
            if segment["speaker_id"]:
                segment["speaker_id"] = source + ":" + segment["speaker_id"]
            segments.append(segment)
    segments.sort(key=lambda segment: (segment["start_ms"], segment["source_id"], segment["end_ms"]))
    for index, segment in enumerate(segments):
        segment["id"] = f"s{index:06d}"
    return Transcript.model_validate({"language": "es", "duration_ms": duration,
        "model_fingerprint": fingerprints[0], "segments": segments}).model_dump()


def process(queue, task, lease, api, state, lock_fd=None):
    policy = json.loads((ROOT / "synth/resources/runtime-policy.json").read_text())
    if policy.get("id") != "operator-configured-gateway-v1":
        raise ValueError("runtime_policy_missing")
    actor = api.call("GET", "/v1/me")
    if task["metadata"].get("owner_id") is not None and task["metadata"]["owner_id"] != actor["id"]:
        raise ValueError("capture_owner_changed")
    if getattr(api,"auth_mode",None)=="supabase" and task["metadata"].get("owner_id") is None:
        raise ValueError("capture_owner_missing")
    if actor["kind"] != "user" or actor["id"] == "00000000-0000-4000-8000-000000000001":
        raise ValueError("desktop_identity_must_be_nonadmin_user")
    identifier, token = task["id"], task["lease_token"]
    checkpoints = task["checkpoints"]
    output = Path(state) / "tasks" / identifier
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = queue.capture_root / ("processed-" + identifier)
    raw.mkdir(parents=True, exist_ok=True, mode=0o700)

    def checkpoint(stage, descriptor):
        lease.check(queue)
        queue.checkpoint(identifier, token, stage, descriptor)
        checkpoints[stage] = descriptor

    if "canonical" in checkpoints:
        canonical = restore_json(checkpoints["canonical"], output)
    else:
        normalized = checkpoints.get("normalized")
        if normalized is None:
            normalized = {}
            for source, descriptor in task["metadata"]["sources"].items():
                lease.check(queue)
                source_path = Path(descriptor["path"]).resolve(strict=True)
                if not source_path.is_relative_to(queue.capture_root) or file_hash(source_path) != descriptor["sha256"]:
                    raise ValueError("capture_file_changed")
                target = raw / (source + ".wav")
                normalize_audio(source_path, target, lock_fd=lock_fd)
                target.chmod(0o600)
                normalized[source] = {"path": str(target), "sha256": file_hash(target)}
            checkpoint("normalized", normalized)
        for descriptor in normalized.values():
            path = Path(descriptor["path"]).resolve(strict=True)
            if not path.is_relative_to(raw) or file_hash(path) != descriptor["sha256"]:
                raise ValueError("normalized_audio_invalid")
        if "asr" in checkpoints:
            transcripts = restore_json(checkpoints["asr"], output)
        else:
            transcripts = {}
            for source, descriptor in normalized.items():
                lease.check(queue)
                transcripts[source], _ = transcribe(descriptor["path"], output / source, source_id=source, lock_fd=lock_fd)
            checkpoint("asr", save_json(output / "asr.json", transcripts))
        if "diarization" in checkpoints:
            diarizations = restore_json(checkpoints["diarization"], output)
        else:
            diarizations = {}
            for source, descriptor in normalized.items():
                lease.check(queue)
                diarizations[source] = infer("diarize", descriptor["path"], raw, lock_fd=lock_fd)
            checkpoint("diarization", save_json(output / "diarization.json", diarizations))
        canonical = canonical_transcript(transcripts, diarizations)
        from .transcript_quality import select_canonical
        canonical, quality = select_canonical(transcripts, canonical, task, queue.capture_root)
        save_json(output / 'transcript-quality.json', quality)
        checkpoint("canonical", save_json(output / "transcript.json", canonical))
    # Pilot comparison is separate from calibrated employee identity. No provisional
    # label/score/vector enters the transcript or external document provider.
    from synth.speakers.matching import identify
    diarizations = restore_json(checkpoints["diarization"], output)
    identity_path = output / "identity-status.json"
    from synth.speakers.meeting_status import transcript_hash
    canonical_hash = transcript_hash(canonical)
    if identity_path.exists():
        identity = json.loads(identity_path.read_text())
        from synth.speakers.matching import profile_signature
        signature, _ = profile_signature(actor)
        if identity.get('transcript_hash') not in (None, canonical_hash):
            identity = {"state":"unavailable","reason":"transcript_version_changed","matches":{}}
        elif identity.get("profile_signature") != signature:
            identity = {"state":"unavailable","reason":"profile_changed_audio_deleted","matches":{}}
    else:
        try:
            identity = identify(actor, checkpoints["normalized"], diarizations, raw, lock_fd)
        except (ValueError, OSError):
            identity = {"state":"unavailable","reason":"voice_comparison_failed","matches":{}}
    identity.update(processing_mode='automatic', transcript_hash=canonical_hash)
    save_json(output / "identity-status.json", identity)
    if 'identified' not in checkpoints:
        checkpoint('identified', {'mode': 'automatic', 'transcript_hash': canonical_hash})
    # A cloud retry never requires keeping raw audio once acoustic processing ends.
    delete_processed_audio(task, queue.capture_root)
    save_json(output / "audio-deletion.json", {"deleted": True, "after": "acoustic_processing", "at": time.time()})
    lease.check(queue)
    if "uploaded" not in checkpoints:
        meeting = api.call("POST", "/v1/meetings", {"title": task["metadata"]["title"],
            "folder_id": task["metadata"]["folder_id"], "idempotency_key": "desktop:" + identifier})
        if task["metadata"].get("notes") and meeting["revision"] == 1:
            lease.check(queue)
            api.call("PUT", f'/v1/meetings/{meeting["id"]}/notes',
                {"notes": task["metadata"]["notes"], "expected_revision": meeting["revision"]})
        lease.check(queue)
        job = api.call("POST", f'/v1/meetings/{meeting["id"]}/transcript',
            {"transcript": canonical, "idempotency_key": "desktop:" + identifier + ":canonical"})
        checkpoint("uploaded", {"meeting_id": meeting["id"], "job_id": job["job_id"],
                                "transcript_version": job.get('transcript_version')})
    upload = checkpoints["uploaded"]
    deadline = time.monotonic() + 600
    retried = False
    while time.monotonic() < deadline:
        lease.check(queue)
        job = api.call("GET", "/v1/jobs/" + upload["job_id"])
        if job["state"] == "succeeded":
            document = api.call("GET", "/v1/meetings/" + upload["meeting_id"] + "/document")
            descriptor = save_json(output / "document.json", document)
            return {**upload, "document": descriptor, "identity": identity,
                    "runtime_policy_id": policy["id"]}
        if job["state"] == "failed" and task["attempts"] > 1 and not retried:
            api.call("POST", "/v1/jobs/" + upload["job_id"] + "/retry")
            retried = True
        elif job["state"] in {"failed", "cancelled"}:
            raise ValueError("document_job_" + job["state"])
        time.sleep(1)
    raise ValueError("document_wait_timeout")


def work_once(database=STATE / "queue.sqlite3", capture=CAPTURE, state=STATE, api=None):
    with inference_lock(state) as lock_fd:
        queue = DesktopQueue(database, capture)
        try:
            task = queue.claim()
            if task is None:
                return None
            with Lease(database, capture, task) as lease:
                try:
                    result = process(queue, task, lease, api or LocalAPI(), state, lock_fd=lock_fd)
                    lease.check(queue)
                    return queue.finish(task["id"], task["lease_token"], result=result)
                except (ValueError, OSError) as error:
                    current = queue.get(task["id"])
                    uploaded = current["checkpoints"].get("uploaded")
                    if current["state"] == "cancel_requested" and uploaded:
                        try:
                            (api or LocalAPI()).call("POST", "/v1/jobs/" + uploaded["job_id"] + "/cancel")
                        except (ValueError, OSError):
                            # Local cancellation still takes effect. The text job
                            # stays inspectable separately if its API is unavailable.
                            pass
                    code = str(error)
                    if not code.replace("_", "").isalnum() or len(code) > 100:
                        code = "host_pipeline_failed"
                    return queue.finish(task["id"], task["lease_token"], error_code=code)
        finally:
            queue.close()
