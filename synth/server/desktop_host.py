"""Authenticated loopback control plane for the Mac acoustic pipeline.

Only task metadata and text-service IDs leave this service. Native Tauri holds
the credential; browsers receive no CORS access and audio never leaves the Mac.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import threading
from uuid import UUID

from synth.worker.desktop_pipeline import APPDATA, CAPTURE, STATE, LocalAPI, work_once
from synth.worker.desktop_queue import DesktopQueue, QueueError
from synth.speakers import onboarding

DATABASE = STATE / "queue.sqlite3"
STOP = threading.Event()
WORKER_STATUS = {"state": "starting", "error_code": None}


def public_task(task):
    result = task.get("result") or task.get("checkpoints", {}).get("uploaded", {})
    return {"id": task["id"], "state": task["state"], "stage": task["stage"],
        "attempts": task["attempts"], "error_code": task["error_code"],
        "created_at": task["created_at"], "updated_at": task["updated_at"],
        "meeting_id": result.get("meeting_id"), "job_id": result.get("job_id"),
        "identity": result.get("identity"), "title": task["metadata"]["title"],
        "folder_id": task["metadata"].get("folder_id"), "capture_id": task["metadata"].get("capture_id")}


def worker():
    while not STOP.is_set():
        try:
            WORKER_STATUS.update(state="running", error_code=None)
            task = work_once()
            if task is None:
                WORKER_STATUS["state"] = "idle"
                STOP.wait(1)
        except Exception:
            # Errors and credentials must not be rendered from exception strings.
            WORKER_STATUS.update(state="recovering", error_code="host_worker_unavailable")
            STOP.wait(5)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def respond(self, status, value):
        payload = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self):
        origin = self.headers.get("Origin")
        if origin and origin not in {"tauri://localhost", "http://tauri.localhost"}:
            self.respond(403, {"detail": "host_origin_denied"})
            return False
        try:
            token = (APPDATA / "desktop-api-key").read_text().strip()
        except OSError:
            self.respond(503, {"detail": "desktop_identity_missing"})
            return False
        value = self.headers.get("Authorization", "")
        if not token or not hmac.compare_digest(value.encode(), ("Bearer " + token).encode()):
            self.respond(401, {"detail": "host_authentication_required"})
            return False
        return True

    def handle_request(self):
        if self.command == "GET" and self.path.startswith("/auth/callback/"):
            from . import desktop_auth
            try:
                desktop_auth.callback(self.path)
                self.respond(200,{"message":"Ya has iniciado sesión. Puedes volver a Synth."})
            except ValueError:
                self.respond(400,{"message":"No se pudo completar el login. Vuelve a Synth e inténtalo otra vez."})
            return
        if not self.authorized():
            return
        queue = None
        try:
            if any(character in self.path for character in ("?", "#", "%", "\\")):
                raise QueueError("host_path_invalid")
            if self.path.startswith("/v1/auth/"):
                from . import desktop_auth
                if self.command=="GET" and self.path=="/v1/auth/state":
                    self.respond(200,desktop_auth.state()); return
                if self.command=="GET" and self.path=="/v1/auth/access":
                    session=desktop_auth.access()
                    self.respond(200,{"access_token":session["access_token"]}); return
                if self.command=="POST" and self.path=="/v1/auth/login":
                    self.respond(200,desktop_auth.begin()); return
                if self.command=="POST" and self.path=="/v1/auth/logout":
                    self.respond(200,desktop_auth.logout()); return
                self.respond(404,{"detail":"host_route_missing"}); return
            if self.command == "GET" and self.path == "/v1/status":
                backend = LocalAPI().base_url
                self.respond(200, {"worker": dict(WORKER_STATUS), "audio_location": "this_mac",
                    "identity": "calibration_pending", "backend_url": backend,
                    "document_route": "configured_gateway"})
                return
            if self.path.startswith("/v1/voice"):
                actor = LocalAPI().call("GET", "/v1/me")
                if actor["kind"] != "user" or actor["id"] == "00000000-0000-4000-8000-000000000001":
                    raise QueueError("desktop_identity_must_be_nonadmin_user")
                if self.command == "POST" and self.path.startswith("/v1/voice/meetings/") and self.path.endswith("/analyze"):
                    from synth.speakers.matching import analyze_existing
                    from synth.worker.desktop_pipeline import save_json
                    from uuid import uuid4
                    identifier=str(UUID(self.path.split("/")[-2])); LocalAPI().call("GET","/v1/meetings/"+identifier)
                    queue=DesktopQueue(DATABASE,CAPTURE)
                    row=queue.db.execute("SELECT * FROM desktop_tasks WHERE json_extract(result,'$.meeting_id')=? AND state='succeeded'",(identifier,)).fetchone()
                    if row is None: raise QueueError("task_missing")
                    task=queue.decode(row)
                    from synth.speakers.meeting_status import audio_available, meeting_status
                    transcript = LocalAPI().call('GET', '/v1/meetings/' + identifier + '/transcript')
                    from synth.speakers.matching import profile_signature
                    signature, _ = profile_signature(actor)
                    current = meeting_status(task, transcript, STATE/'tasks'/task['id'], signature)
                    if not audio_available(task):
                        self.respond(200, current); return
                    if current.get('reason') == 'transcript_version_changed':
                        self.respond(200, current); return
                    if not onboarding._GATE.acquire(blocking=False): raise QueueError("voice_busy")
                    job={"id":str(uuid4()),"employee_id":actor["id"],"operation":"meeting","state":"running","created_at":__import__('time').time(),"error_code":None,"result":None}
                    onboarding.JOBS.mkdir(parents=True,exist_ok=True,mode=0o700)
                    try: save_json(onboarding.JOBS/(job['id']+'.json'),job)
                    except BaseException: onboarding._GATE.release(); raise
                    def analyze():
                        try: job.update(state="succeeded",result=analyze_existing(actor,task))
                        except Exception: job.update(state="failed",error_code="meeting_voice_analysis_failed")
                        finally:
                            try: save_json(onboarding.JOBS/(job['id']+'.json'),job)
                            finally: onboarding._GATE.release()
                    threading.Thread(target=analyze,daemon=True).start()
                    self.respond(202,{"id":job['id'],"state":"running"}); return
                if self.command == "GET" and self.path.startswith("/v1/voice/meetings/"):
                    from synth.speakers.matching import profile_signature
                    identifier = str(UUID(self.path.split("/")[-1]))
                    api=LocalAPI(); api.call("GET", "/v1/meetings/"+identifier)
                    transcript=api.call("GET", "/v1/meetings/"+identifier+"/transcript")
                    queue=DesktopQueue(DATABASE,CAPTURE)
                    row=queue.db.execute("SELECT * FROM desktop_tasks WHERE json_extract(result,'$.meeting_id')=? OR json_extract(checkpoints,'$.uploaded.meeting_id')=? ORDER BY created_at DESC LIMIT 1",(identifier,identifier)).fetchone()
                    if row is None: self.respond(200,{"state":"unavailable","reason":"native_audio_unavailable","matches":{}}); return
                    task=queue.decode(row); output=STATE/"tasks"/task["id"]
                    from synth.speakers.meeting_status import meeting_status
                    signature,count=profile_signature(actor)
                    self.respond(200, meeting_status(task, transcript, output, signature)); return
                if self.command == "GET" and self.path == "/v1/voice":
                    self.respond(200, onboarding.status(actor)); return
                if self.command == "POST" and self.path == "/v1/voice/share":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= length <= 4096 or self.headers.get("Transfer-Encoding"): raise QueueError("host_body_invalid")
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict) or set(body) != {"profile_id", "consent_confirmed"}: raise QueueError("host_body_invalid")
                    from synth.speakers.sync import share
                    self.respond(200, share(actor, body["profile_id"], body["consent_confirmed"])); return
                if self.command == "GET" and self.path.startswith("/v1/voice/jobs/"):
                    self.respond(200, onboarding.read_job(actor, self.path.split("/")[-1])); return
                if self.command == "POST" and self.path in {"/v1/voice/enroll", "/v1/voice/test", "/v1/voice/delete"}:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= length <= 4096 or self.headers.get("Transfer-Encoding"): raise QueueError("host_body_invalid")
                    body = json.loads(self.rfile.read(length))
                    allowed = {"profile_id"} if self.path.endswith("delete") else {"recording_id", "consent_confirmed"}
                    if not isinstance(body, dict) or set(body) - allowed: raise QueueError("host_body_invalid")
                    if self.path.endswith("delete"):
                        self.respond(200, onboarding.delete(actor,body.get("profile_id"))); return
                    self.respond(202,onboarding.submit(actor,body.get("recording_id"),body.get("consent_confirmed"),self.path.split("/")[-1])); return
                self.respond(404,{"detail":"host_route_missing"}); return
            queue = DesktopQueue(DATABASE, CAPTURE)
            if self.command == "GET" and self.path == "/v1/tasks":
                rows = queue.db.execute("SELECT * FROM desktop_tasks ORDER BY created_at DESC LIMIT 100").fetchall()
                actor = LocalAPI().call("GET", "/v1/me")
                rows = [row for row in rows if queue.decode(row)["metadata"].get("owner_id")==actor["id"]]
                from .task_status import project_task
                api = LocalAPI()
                self.respond(200, {"items": [project_task(public_task(queue.decode(row)), api) for row in rows]})
                return
            if self.command == "POST" and self.path == "/v1/captures":
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 1024 * 1024 or self.headers.get("Transfer-Encoding"):
                    raise QueueError("host_body_invalid")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body) - {"sources", "title", "folder_id", "consent_confirmed", "capture_id", "notes"}:
                    raise QueueError("host_body_invalid")
                actor = LocalAPI().call("GET", "/v1/me")
                if actor["kind"] != "user" or actor["id"] == "00000000-0000-4000-8000-000000000001":
                    raise QueueError("desktop_identity_must_be_nonadmin_user")
                task = queue.enqueue(body.get("sources"), body.get("title"), body.get("folder_id"), body.get("consent_confirmed"), body.get("capture_id"), body.get("notes", ""), owner_id=actor["id"])
                self.respond(201, public_task(task))
                return
            parts = self.path.split("/")
            if len(parts) not in {4, 5} or parts[1:3] != ["v1", "tasks"]:
                self.respond(404, {"detail": "host_route_missing"})
                return
            identifier = str(UUID(parts[3]))
            existing = queue.get(identifier)
            actor = LocalAPI().call("GET", "/v1/me")
            if existing["metadata"].get("owner_id")!=actor["id"]:
                raise QueueError("task_missing")
            if self.command == "GET" and len(parts) == 4:
                task = queue.get(identifier)
            elif self.command == "POST" and len(parts) == 5 and parts[4] in {"retry", "cancel"}:
                from .task_status import document_action
                task = existing if document_action(LocalAPI(), existing, parts[4]) else getattr(queue, parts[4])(identifier)
            else:
                self.respond(404, {"detail": "host_route_missing"})
                return
            from .task_status import project_task
            self.respond(200, project_task(public_task(task), LocalAPI()))
        except QueueError as error:
            self.respond(404 if str(error) == "task_missing" else 409, {"detail": str(error)})
        except ValueError as error:
            code = str(error)
            self.respond(409, {"detail": code if code.replace("_", "").isalnum() and len(code) < 100 else "host_request_failed"})
        except (TypeError, OSError):
            self.respond(400, {"detail": "host_request_failed"})
        finally:
            if queue is not None:
                queue.close()

    do_GET = handle_request
    do_POST = handle_request


def main():
    os.umask(0o077)
    CAPTURE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Bind before starting any inference; a second daemon cannot consume tasks.
    server = ThreadingHTTPServer(("127.0.0.1", 18383), Handler)
    onboarding.recover_interrupted_jobs()
    threading.Thread(target=worker, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        STOP.set()
        server.server_close()


if __name__ == "__main__":
    main()
