"""Authenticated loopback control plane for the Mac acoustic pipeline.

Only task metadata and text-service IDs leave this service. Native Tauri holds
the credential; browsers receive no CORS access and audio never leaves the Mac.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import hashlib
import sys
import threading
from pathlib import Path
from uuid import UUID

from synth.worker.desktop_pipeline import APPDATA, CAPTURE, STATE, LocalAPI, work_once
from synth.worker.desktop_queue import DesktopQueue, QueueError
from synth.speakers import onboarding
from . import audio_setup

DATABASE = STATE / "queue.sqlite3"
STOP = threading.Event()
CONTEXT_LOCK = threading.Lock()
WORKER_STATUS = {"state": "starting", "error_code": None}
_BUILD_FILE=Path(__file__).resolve().parents[2]/'host-build.json'
HOST_BUILD=json.loads(_BUILD_FILE.read_text())['id'] if _BUILD_FILE.is_file() else 'development'
with Path(sys.executable).open('rb') as _host_binary:
    HOST_BINARY_SHA256 = hashlib.file_digest(_host_binary, 'sha256').hexdigest()


def public_task(task):
    from .task_status import pending_voice_confirmation
    pending_voice = pending_voice_confirmation(task)
    result = task.get("result") or task.get("checkpoints", {}).get("uploaded", {})
    # Existing desktop clients call both normalization and echo cancellation
    # "Audio preparado". Keep their progress display compatible with this host.
    stage = 'normalized' if task['stage'] == 'acoustic' else ('asr' if task['stage'].startswith('asr_') else task['stage'])
    if pending_voice:
        stage = 'confirming_voices'
    return {"id": task["id"], "state": task["state"], "stage": stage,
        "attempts": task["attempts"], "error_code": task["error_code"],
        "created_at": task["created_at"], "updated_at": task["updated_at"],
        "meeting_id": result.get("meeting_id"), "job_id": result.get("job_id"),
        "identity": result.get("identity"), "title": task["metadata"]["title"],
        "voice_confirmation_pending": pending_voice,
        "folder_id": task["metadata"].get("folder_id"), "capture_id": task["metadata"].get("capture_id")}


def worker():
    while not STOP.is_set():
        try:
            if not audio_setup.ready():
                WORKER_STATUS.update(state='preparing_audio',error_code=None)
                STOP.wait(1)
                continue
            WORKER_STATUS.update(state="running", error_code=None)
            task = work_once()
            if task is None:
                WORKER_STATUS["state"] = "idle"
                STOP.wait(1)
        except ValueError as error:
            if str(error) == 'acoustic_worker_already_running':
                WORKER_STATUS.update(state='audio_in_use',error_code=None)
                STOP.wait(1)
                continue
            WORKER_STATUS.update(state='recovering',error_code='host_worker_unavailable')
            STOP.wait(5)
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
        if status < 400 and getattr(self, 'request_api', None) is not None:
            self.request_api.assert_current_principal()
        payload = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def browser_callback(self, status, *, calendar=False, code=None):
        from html import escape
        if status < 400:
            title = 'Calendario conectado' if calendar else 'Ya has iniciado sesión'
            detail = 'Vuelve a Synth para ver tus reuniones.'
        elif code == 'calendar_client_configuration_invalid':
            title = 'Falta configurar la conexión con Google'
            detail = 'Tu sesión de Synth sigue abierta. Hay que configurar el secreto original del cliente de Google antes de volver a conectar el calendario.'
        elif code == 'calendar_api_not_enabled':
            title = 'Falta habilitar Google Calendar'
            detail = 'Tu sesión de Synth sigue abierta. Hay que habilitar Calendar API en el proyecto de Google de Synth.'
        else:
            title = 'No se pudo conectar el calendario' if calendar else 'No se pudo completar el acceso'
            detail = 'Vuelve a Synth para revisar el estado de la conexión.'
        # No assets, scripts, copied callback URL or provider error text. Keep the
        # one-use authorization code out of referrers and browser resource requests.
        payload = ('''<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
            <title>Synth · Google</title><style>
            :root {color-scheme:light dark;font-family:system-ui,sans-serif;color:#25231d;background:#f6f6f3}
            body{margin:0;min-height:100svh;display:grid;place-items:center}main{max-width:38rem;padding:2rem}
            .brand{color:#a93636;font-weight:600}h1{font-size:clamp(1.5rem,4vw,2rem);font-weight:500;line-height:1.2}
            p{line-height:1.6;max-width:34rem}small{color:#68615a}
            @media(prefers-color-scheme:dark){:root{color:#ece9e3;background:#1c1b17}.brand{color:#e08a8a}small{color:#a39a8f}}
            </style><main><p class="brand">Synth <small>by Synth</small></p><h1>'''+escape(title)+
            '</h1><p>'+escape(detail)+'</p><small>Puedes cerrar esta pestaña.</small></main></html>').encode()
        self.send_response(status)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Content-Length',str(len(payload)))
        self.send_header('Cache-Control','no-store')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers();self.wfile.write(payload)

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
            calendar = bool(desktop_auth.FLOW and desktop_auth.FLOW.get('calendar'))
            try:
                desktop_auth.callback(self.path)
                self.browser_callback(200, calendar=calendar)
            except ValueError as error:
                self.browser_callback(400, calendar=calendar, code=str(error))
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
                if self.command=="GET" and self.path=="/v1/auth/capture-identity":
                    self.respond(200,desktop_auth.capture_identity()); return
                if self.command=="GET" and self.path=="/v1/auth/access":
                    session=desktop_auth.access()
                    self.respond(200,{"access_token":session["access_token"]}); return
                if self.command=="POST" and self.path=="/v1/auth/login":
                    self.respond(200,desktop_auth.begin()); return
                if self.command=="POST" and self.path=="/v1/auth/calendar":
                    self.respond(200,desktop_auth.begin(calendar=True)); return
                if self.command=="POST" and self.path=="/v1/auth/logout":
                    self.respond(200,desktop_auth.logout()); return
                self.respond(404,{"detail":"host_route_missing"}); return
            if self.command == "GET" and self.path == "/v1/status":
                backend = LocalAPI().base_url
                from synth.worker.qwen_asr import CONFIG
                acoustic_config = json.loads(CONFIG.read_text()) if CONFIG.is_file() else {}
                asr = acoustic_config.get('backend', 'whisper')
                from synth.speakers.matching import PILOT_THRESHOLD
                self.respond(200, {"worker": dict(WORKER_STATUS), "audio_location": "this_mac",
                    "host_build":HOST_BUILD, "host_binary_sha256":HOST_BINARY_SHA256,
                    "audio_setup":audio_setup.status(),
                    "asr_backend": asr,
                    "echo_cancellation": acoustic_config.get('echo_cancellation', 'off'),
                    "voice_embedding_space": acoustic_config.get('voice_embedding_space','cosine256'),
                    "voice_pilot_threshold": PILOT_THRESHOLD,
                    "identity": "calibration_pending", "backend_url": backend,
                    "document_route": "configured_gateway"})
                return
            if self.command=='POST' and self.path=='/v1/audio/setup/retry':
                self.respond(202,audio_setup.start());return
            if self.command == 'POST' and self.path == '/v1/capture/context':
                import time
                from synth.worker.consolidated_capture import atomic_json
                from synth.worker.capture_vocabulary import vocabulary
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192:
                    raise QueueError('capture_context_invalid')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body)-{'calendar_event_id','folder_id'}:
                    raise QueueError('capture_context_invalid')
                event_id = str(UUID(body['calendar_event_id'])) if body.get('calendar_event_id') else None
                folder_id = str(UUID(body['folder_id'])) if body.get('folder_id') else None
                pending_path = STATE/'prepared-capture-context.json'
                ticket = os.urandom(16).hex()
                with CONTEXT_LOCK:
                    atomic_json(pending_path, {'owner':None,'created_at':time.time(),'terms':[],'ticket':ticket})
                from . import desktop_auth
                identity = desktop_auth.capture_identity()
                owner = identity.get('principal_id')
                if identity.get('mode') == 'keys':
                    owner = 'keys:'+hashlib.sha256((APPDATA/'desktop-api-key').read_text().strip().encode()).hexdigest()
                if not owner:
                    raise QueueError('capture_context_owner_missing')
                # Clear the previous note's hints before any network operation.
                # An offline start must use no context, never stale private terms.
                api = LocalAPI()
                actor = api.call('GET', '/v1/me')
                self.request_api = api
                if event_id:
                    pack = api.call('PUT', '/v1/calendar/events/'+event_id+'/folder', {'folder_id':folder_id})
                    if pack['folder_id'] != folder_id:
                        raise QueueError('capture_context_folder_changed')
                    terms = [term['term'] for term in pack['glossary']]
                else:
                    terms = api.call('GET', '/v1/glossary?status=aprobado'+('&folder_id='+folder_id if folder_id else ''))['items']
                    terms = [term['term'] for term in terms[:40]]
                terms = vocabulary([term for term in terms if len(term)<=60])
                api.assert_current_principal()
                with CONTEXT_LOCK:
                    if not pending_path.exists() or json.loads(pending_path.read_text()).get('ticket') != ticket:
                        self.respond(200, {'prepared':False,'terms':0}); return
                    atomic_json(pending_path, {'owner':owner,'created_at':time.time(),'terms':terms,'ticket':ticket})
                self.respond(200, {'prepared':True,'terms':len(terms)}); return
            if self.path.startswith("/v1/voice"):
                api = LocalAPI()
                actor = api.call("GET", "/v1/me")
                self.request_api = api
                api.assert_current_principal()
                if actor["kind"] != "user" or actor["id"] == "00000000-0000-4000-8000-000000000001":
                    raise QueueError("desktop_identity_must_be_nonadmin_user")
                if self.path.startswith('/v1/voice/captures/'):
                    from synth.speakers.live_status import capture_status, confirm_capture
                    import hashlib
                    parts = self.path.split('/')
                    if not ((self.command == 'GET' and len(parts) == 5) or
                            (self.command == 'POST' and len(parts) == 6 and parts[-1] == 'speakers')):
                        raise QueueError('host_path_invalid')
                    identifier = str(UUID(parts[4]))
                    identity = actor['id'] if api.auth_mode == 'supabase' else 'keys:' + hashlib.sha256((APPDATA/'desktop-api-key').read_text().strip().encode()).hexdigest()
                    if self.command == 'POST':
                        length = int(self.headers.get('Content-Length', '0'))
                        if not 1 <= length <= 4096 or self.headers.get('Transfer-Encoding'):
                            raise QueueError('host_body_invalid')
                        body = json.loads(self.rfile.read(length))
                        self.respond(200, confirm_capture(identifier, identity, actor, STATE, CAPTURE, body,
                                                         guard=api.assert_current_principal))
                    else:
                        self.respond(200, capture_status(identifier, identity, actor, STATE, CAPTURE))
                    return
                if self.command == "POST" and self.path.startswith("/v1/voice/meetings/") and self.path.endswith("/analyze"):
                    from synth.speakers.matching import analyze_existing
                    from synth.worker.desktop_pipeline import save_json
                    from uuid import uuid4
                    identifier=str(UUID(self.path.split("/")[-2])); api.call("GET","/v1/meetings/"+identifier)
                    queue=DesktopQueue(DATABASE,CAPTURE)
                    row=queue.db.execute("SELECT * FROM desktop_tasks WHERE json_extract(result,'$.meeting_id')=? AND state='succeeded'",(identifier,)).fetchone()
                    if row is None: raise QueueError("task_missing")
                    task=queue.decode(row)
                    from synth.speakers.meeting_status import audio_available, meeting_status
                    transcript = api.call('GET', '/v1/meetings/' + identifier + '/transcript')
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
                if self.command == "POST" and self.path.startswith("/v1/voice/meetings/") and self.path.endswith("/speakers"):
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= length <= 4096 or self.headers.get("Transfer-Encoding"): raise QueueError("host_body_invalid")
                    body = json.loads(self.rfile.read(length))
                    identifier = str(UUID(self.path.split("/")[-2]))
                    queue = DesktopQueue(DATABASE, CAPTURE)
                    row = queue.db.execute("SELECT id FROM desktop_tasks WHERE json_extract(result,'$.meeting_id')=? OR json_extract(checkpoints,'$.uploaded.meeting_id')=? ORDER BY created_at DESC LIMIT 1", (identifier, identifier)).fetchone()
                    from synth.speakers.voice_actions import resolve_speaker
                    self.respond(201, resolve_speaker(LocalAPI(), identifier, row["id"] if row else None, body)); return
                if self.command == "GET" and self.path.startswith("/v1/voice/meetings/"):
                    from synth.speakers.matching import profile_signature
                    identifier = str(UUID(self.path.split("/")[-1]))
                    api.call("GET", "/v1/meetings/"+identifier)
                    transcript=api.call("GET", "/v1/meetings/"+identifier+"/transcript")
                    queue=DesktopQueue(DATABASE,CAPTURE)
                    row=queue.db.execute("SELECT * FROM desktop_tasks WHERE json_extract(result,'$.meeting_id')=? OR json_extract(checkpoints,'$.uploaded.meeting_id')=? ORDER BY created_at DESC LIMIT 1",(identifier,identifier)).fetchone()
                    if row is None: self.respond(200,{"state":"unavailable","reason":"native_audio_unavailable","matches":{}}); return
                    task=queue.decode(row); output=STATE/"tasks"/task["id"]
                    from synth.speakers.meeting_status import meeting_status
                    from synth.speakers.matching import reidentify
                    signature,count=profile_signature(actor)
                    def fresh():
                        try:
                            return reidentify(actor, task["id"], participants=set(task["metadata"].get("participants") or []) or None)
                        except Exception as error:  # never a 500 for a voice status read
                            from synth.speakers.matching import failure_cause
                            return {"state": "unavailable", "reason": "voice_comparison_failed", "cause": failure_cause(error), "matches": {}}
                    self.respond(200, meeting_status(task, transcript, output, signature, reidentify=fresh)); return
                if self.command == 'GET' and self.path == '/v1/voice/learning':
                    from synth.speakers.learning import status
                    self.respond(200,status(actor,api)); return
                if self.command == 'POST' and self.path in ('/v1/voice/learning/settings','/v1/voice/learning/sync'):
                    from synth.speakers.learning import set_enabled,status,synchronize
                    if self.path.endswith('/settings'):
                        length=int(self.headers.get('Content-Length','0'))
                        if not 1<=length<=1024 or self.headers.get('Transfer-Encoding'):raise QueueError('host_body_invalid')
                        body=json.loads(self.rfile.read(length))
                        if not isinstance(body,dict) or set(body)!={'enabled'}:raise QueueError('host_body_invalid')
                        api.assert_current_principal()
                        self.respond(200,set_enabled(actor,body['enabled'],api)); return
                    import threading
                    threading.Thread(target=synchronize,args=(dict(actor),),daemon=True).start()
                    self.respond(202,status(actor,api)); return
                if self.path.startswith('/v1/voice/samples/'):
                    parts=self.path.split('/')
                    valid=(self.command=='POST' and len(parts)==6 and parts[-1]=='accept') or (self.command=='DELETE' and len(parts)==5)
                    if not valid:raise QueueError('host_path_invalid')
                    str(UUID(parts[4]));api.assert_current_principal()
                    self.respond(200,api.call(self.command,self.path)); return
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
                api = LocalAPI()
                actor = api.call("GET", "/v1/me")
                rows = queue.list_owned(actor['id'])
                from .task_status import project_tasks
                items = project_tasks([public_task(queue.decode(row)) for row in rows], api)
                api.assert_current_principal()
                self.respond(200, {"items": items})
                return
            if self.command == "POST" and self.path == "/v1/captures":
                if not audio_setup.ready():raise QueueError('audio_setup_not_ready')
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 1024 * 1024 or self.headers.get("Transfer-Encoding"):
                    raise QueueError("host_body_invalid")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body) - {"sources", "title", "folder_id", "consent_confirmed", "capture_id", "notes", "eval_retention", "participants", "calendar_event_id"}:
                    raise QueueError("host_body_invalid")
                api = LocalAPI()
                actor = api.call("GET", "/v1/me")
                if actor["kind"] != "user" or actor["id"] == "00000000-0000-4000-8000-000000000001":
                    raise QueueError("desktop_identity_must_be_nonadmin_user")
                api.assert_current_principal()
                from .capture_owner import validate_sources
                import hashlib
                identity = actor["id"] if api.auth_mode == "supabase" else "keys:" + hashlib.sha256((APPDATA/"desktop-api-key").read_text().strip().encode()).hexdigest()
                validate_sources(body.get("sources"), body.get("capture_id"), identity, CAPTURE)
                task = queue.enqueue(body.get("sources"), body.get("title"), body.get("folder_id"), body.get("consent_confirmed"), body.get("capture_id"), body.get("notes", ""), owner_id=actor["id"], eval_retention=body.get("eval_retention", False), participants=body.get("participants"), calendar_event_id=body.get('calendar_event_id'))
                api.assert_current_principal()
                self.respond(201, public_task(task))
                return
            parts = self.path.split("/")
            if len(parts) not in {4, 5} or parts[1:3] != ["v1", "tasks"]:
                self.respond(404, {"detail": "host_route_missing"})
                return
            identifier = str(UUID(parts[3]))
            existing = queue.get(identifier)
            api = LocalAPI()
            actor = api.call("GET", "/v1/me")
            if existing["metadata"].get("owner_id")!=actor["id"]:
                raise QueueError("task_missing")
            if self.command == "GET" and len(parts) == 4:
                task = queue.get(identifier)
            elif self.command == "POST" and len(parts) == 5 and parts[4] in {"retry", "cancel"}:
                from .task_status import document_action
                api.assert_current_principal()
                task = existing if document_action(api, existing, parts[4]) else getattr(queue, parts[4])(identifier)
            else:
                self.respond(404, {"detail": "host_route_missing"})
                return
            from .task_status import project_task
            view = project_task(public_task(task), api)
            api.assert_current_principal()
            self.respond(200, view)
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
    audio_setup.start()
    threading.Thread(target=worker, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        STOP.set()
        server.server_close()


if __name__ == "__main__":
    main()
