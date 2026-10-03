"""Local pilot REST API. Bearer keys never appear in URLs or application logs."""
from contextlib import asynccontextmanager
import os
import secrets
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response, JSONResponse
from fastapi.encoders import jsonable_encoder
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import json
from .db import digest
from synth.contracts.transcript import Transcript
from synth.worker.queue import submit, cancel

from .db import audit, authenticate, authorize_folder, authorize_meeting, connect, digest, migrate
from .supabase_auth import auth_mode, allowed_domains, configuration, verified_user, verify_live_session
from .voice_registry import Enrollment, seal, unseal


@asynccontextmanager
async def lifespan(app):
    mode = os.environ.get('SYNTH_RUN_MIGRATIONS', '1')
    if mode not in {'0', '1'}: raise ValueError('migration_mode_invalid')
    if mode == '1':
        migrate()
    else:
        with connect() as db:
            required = ['principals', 'access_keys', 'meetings', 'transcript_versions', 'document_versions',
                        'jobs', 'folders', 'auth_identities', 'voice_profiles']
            for name in required:
                if db.execute('SELECT to_regclass(%s) AS relation', (name,)).fetchone()['relation'] is None:
                    raise ValueError('database_schema_initialization_required')
    yield


app = FastAPI(title=os.environ.get("PRODUCT_NAME", "Synth"), version="0.1.0", lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


@app.middleware("http")
async def protect_local_mcp(request: Request, call_next):
    if request.url.path == "/mcp":
        version = request.headers.get("MCP-Protocol-Version")
        if version and version not in {"2025-03-26", "2025-06-18", "2025-11-25"}:
            return JSONResponse({"detail":"MCP protocol version unsupported"},status_code=400)
        origin = request.headers.get("origin")
        if origin and origin not in ("http://127.0.0.1:18280", "http://localhost:18280", "tauri://localhost", "http://tauri.localhost"):
            return Response(status_code=403)
    response = await call_next(request)
    if request.url.path.startswith("/v1/") or request.url.path == "/mcp":
        response.headers["Cache-Control"] = "no-store"
        if response.status_code == 401: response.headers["WWW-Authenticate"] = "Bearer"
    return response


def session(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    with connect() as db:
        actor = authenticate(db, credentials.credentials if credentials else None)
        yield db, actor


# Commit before returning a response: a newly issued key must be immediately usable.
Session = Annotated[tuple, Depends(session, scope="function")]


class Name(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class Principal(Name):
    kind: str = Field(pattern="^(user|machine)$")


class Membership(BaseModel):
    principal_id: UUID
    role: str = Field(pattern="^(reader|editor)$")


class KeyRequest(BaseModel):
    principal_id: UUID
    folder_ids: list[UUID] = Field(default_factory=list, max_length=100)


class IntegrationRequest(Name):
    folder_ids: list[UUID] = Field(min_length=1, max_length=100)


class Meeting(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    folder_id: UUID | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class Notes(BaseModel):
    notes: str = Field(max_length=200_000)
    expected_revision: int = Field(ge=1)


class Assignment(BaseModel):
    folder_id: UUID | None = None


class TranscriptUpload(BaseModel):
    transcript: Transcript
    idempotency_key: str = Field(min_length=1, max_length=200)


class RPC(BaseModel):
    jsonrpc: str = Field(pattern="^2.0$")
    id: str | int | None = None
    method: str = Field(max_length=100)
    params: dict = Field(default_factory=dict)


def require_user(actor):
    if actor["kind"] != "user":
        raise HTTPException(403, "Las integraciones solo pueden leer carpetas compartidas.")


def require_admin(actor):
    if str(actor["id"]) != "00000000-0000-4000-8000-000000000001":
        raise HTTPException(403, "Se necesita el administrador local.")


@app.get("/health")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ready", "ready": True,
            "build_fingerprint": os.environ.get("BUILD_FINGERPRINT", "development")}


@app.get("/v1/auth/config")
def auth_configuration():
    mode = auth_mode()
    if mode == "keys":
        return {"mode": mode}
    url, key = configuration()
    return {"mode": mode, "supabase_url": url, "publishable_key": key,
            "allowed_domains": allowed_domains()}


class LegacyLink(BaseModel):
    legacy_key: str = Field(min_length=20, max_length=200)


@app.post("/v1/auth/link")
def link_existing_library(body: LegacyLink, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if auth_mode() != "supabase" or not credentials:
        raise HTTPException(401, "Inicia sesión con tu cuenta corporativa.")
    identity = verified_user(credentials.credentials)
    with connect() as db:
        verify_live_session(db,identity,credentials.credentials)
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(identity["id"]),))
        old = db.execute("""SELECT p.id,p.name,p.kind,k.id AS key_id FROM access_keys k
            JOIN principals p ON p.id=k.principal_id WHERE k.token_hash=%s
            AND k.revoked_at IS NULL AND p.revoked_at IS NULL FOR UPDATE OF k,p""", (digest(body.legacy_key),)).fetchone()
        if not old or old["kind"] != "user" or str(old["id"]) == "00000000-0000-4000-8000-000000000001":
            raise HTTPException(401, "La credencial de la biblioteca no es válida.")
        linked = db.execute("SELECT subject,principal_id FROM auth_identities WHERE subject=%s OR principal_id=%s", (identity["id"],old["id"])).fetchall()
        if linked and any(r["subject"] != identity["id"] or r["principal_id"] != old["id"] for r in linked):
            raise HTTPException(409, "La biblioteca ya está vinculada a otra identidad.")
        db.execute("INSERT INTO auth_identities(provider,subject,principal_id) VALUES ('supabase',%s,%s) ON CONFLICT DO NOTHING", (identity["id"],old["id"]))
        db.execute("UPDATE access_keys SET revoked_at=now() WHERE principal_id=%s", (old["id"],))
        audit(db, old["id"], "auth.link", old["id"], {"provider":"supabase"})
        return {"id":old["id"],"name":old["name"],"kind":"user","email":identity["email"]}


@app.get("/v1/me")
def current_identity(context: Session):
    _, actor = context
    return {"id": actor["id"], "name": actor["name"], "kind": actor["kind"]}


@app.put("/v1/voice/profiles")
def enroll_voice(body: Enrollment, context: Session):
    db, actor = context
    require_user(actor)
    if body.consent_confirmed is not True:
        raise HTTPException(422, "Confirma compartir tu perfil con Synth.")
    db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("voice:" + str(body.id),))
    old = db.execute("SELECT owner_id FROM voice_profiles WHERE id=%s", (body.id,)).fetchone()
    if old and old["owner_id"] != actor["id"]:
        raise HTTPException(404, "El perfil no está disponible.")
    nonce, encrypted = seal(body, actor["id"])
    # A single active centroid per employee prevents stale enrollment ambiguity.
    db.execute("DELETE FROM voice_profiles WHERE owner_id=%s AND id<>%s", (actor["id"], body.id))
    db.execute("""INSERT INTO voice_profiles(id,owner_id,model,nonce,ciphertext) VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT(id) DO UPDATE SET model=EXCLUDED.model,nonce=EXCLUDED.nonce,
        ciphertext=EXCLUDED.ciphertext,updated_at=now()""", (body.id, actor["id"], body.model, nonce, encrypted))
    audit(db, actor["id"], "voice.enroll", body.id, {"model": body.model})
    return {"id": body.id, "shared": True, "audio_stored": False}


@app.get("/v1/voice/profiles")
def corporate_voice_profiles(context: Session):
    db, actor = context
    require_user(actor)
    rows = db.execute("""SELECT v.*,p.name FROM voice_profiles v JOIN principals p ON p.id=v.owner_id
        LEFT JOIN auth_identities a ON a.principal_id=p.id
        WHERE p.revoked_at IS NULL AND (%s='keys' OR a.principal_id IS NOT NULL)
        ORDER BY v.id LIMIT 501""", (auth_mode(),)).fetchall()
    if len(rows) > 500:
        raise HTTPException(503, "El registro supera el límite del piloto.")
    audit(db, actor["id"], "voice.sync", None, {"profiles": len(rows)})
    return {"profiles": [{"id": r["id"], "employee_id": r["owner_id"], "name": r["name"],
        "model": r["model"], "embedding": unseal(r), "updated_at": r["updated_at"]} for r in rows],
        "cache_ttl_seconds": 300, "audio_stored": False}


@app.delete("/v1/voice/profiles/{profile_id}")
def delete_voice_profile(profile_id: UUID, context: Session):
    db, actor = context
    require_user(actor)
    row = db.execute("DELETE FROM voice_profiles WHERE id=%s AND owner_id=%s RETURNING id", (profile_id, actor["id"])).fetchone()
    if not row:
        raise HTTPException(404, "El perfil no está disponible.")
    audit(db, actor["id"], "voice.delete", profile_id)
    return {"deleted": True}


@app.get("/v1/people")
def people(context: Session):
    db, actor = context
    require_user(actor)
    return {"items": db.execute("SELECT id,name FROM principals WHERE kind='user' AND revoked_at IS NULL AND id<>%s ORDER BY name,id", ("00000000-0000-4000-8000-000000000001",)).fetchall()}


@app.post("/v1/principals", status_code=201)
def create_principal(body: Principal, context: Session):
    db, actor = context
    require_admin(actor)
    pid = uuid4()
    db.execute("INSERT INTO principals(id,name,kind) VALUES (%s,%s,%s)", (pid, body.name, body.kind))
    audit(db, actor["id"], "principal.create", pid, {"kind": body.kind})
    return {"id": pid, "name": body.name, "kind": body.kind}


@app.post("/v1/keys", status_code=201)
def create_key(body: KeyRequest, context: Session):
    db, actor = context
    require_user(actor)
    principal = db.execute("SELECT id,kind FROM principals WHERE id=%s AND revoked_at IS NULL", (body.principal_id,)).fetchone()
    if not principal:
        raise HTTPException(404, "El usuario no está disponible.")
    if principal["kind"] == "user":
        if actor["id"] != principal["id"]:
            require_admin(actor)
        if body.folder_ids:
            raise HTTPException(422, "Las claves de usuario no admiten ámbitos de integración.")
    elif not body.folder_ids:
        raise HTTPException(422, "Una integración necesita carpetas explícitas.")
    for folder in body.folder_ids:
        folder_row = authorize_folder(db, actor, folder, True)
        if folder_row["owner_id"] != actor["id"]:
            raise HTTPException(403, "Solo el propietario puede conceder acceso a una integración.")
        db.execute("INSERT INTO folder_memberships VALUES (%s,%s,'reader') ON CONFLICT DO NOTHING", (folder, principal["id"]))
    key_id, token = uuid4(), secrets.token_urlsafe(32)
    db.execute("INSERT INTO access_keys(id,principal_id,token_hash,issued_by) VALUES (%s,%s,%s,%s)", (key_id, principal["id"], digest(token), actor["id"]))
    for folder in body.folder_ids:
        db.execute("INSERT INTO key_folder_scopes VALUES (%s,%s)", (key_id, folder))
    audit(db, actor["id"], "key.create", key_id, {"principal_id": str(principal["id"])})
    return {"id": key_id, "token": token}


@app.delete("/v1/keys/{key_id}")
def revoke_key(key_id: UUID, context: Session):
    db, actor = context
    require_user(actor)
    row = db.execute("SELECT principal_id,issued_by FROM access_keys WHERE id=%s", (key_id,)).fetchone()
    if not row:
        raise HTTPException(404, "La clave no está disponible.")
    if row["principal_id"] != actor["id"] and row["issued_by"] != actor["id"]:
        require_admin(actor)
    db.execute("UPDATE access_keys SET revoked_at=now() WHERE id=%s", (key_id,))
    audit(db, actor["id"], "key.revoke", key_id)
    return {"revoked": True}


@app.post("/v1/integrations", status_code=201)
def create_integration(body: IntegrationRequest, context: Session):
    db, actor = context
    require_user(actor)
    folders = list(dict.fromkeys(body.folder_ids))
    for fid in folders:
        folder = authorize_folder(db, actor, fid, True)
        if folder["owner_id"] != actor["id"]:
            raise HTTPException(403, "Solo puedes integrar carpetas de tu propiedad.")
    pid, kid, token = uuid4(), uuid4(), secrets.token_urlsafe(32)
    db.execute("INSERT INTO principals(id,name,kind) VALUES (%s,%s,'machine')", (pid, body.name))
    db.execute("INSERT INTO access_keys(id,principal_id,token_hash,issued_by) VALUES (%s,%s,%s,%s)",
               (kid, pid, digest(token), actor["id"]))
    for fid in folders:
        db.execute("INSERT INTO folder_memberships(folder_id,principal_id,role) VALUES (%s,%s,'reader')", (fid, pid))
        db.execute("INSERT INTO key_folder_scopes(key_id,folder_id) VALUES (%s,%s)", (kid, fid))
    audit(db, actor["id"], "key.create", kid, {"principal_id": str(pid), "kind": "integration"})
    return {"id": kid, "name": body.name, "token": token, "folder_ids": folders, "read_only": True}


@app.get("/v1/integrations")
def list_integrations(context: Session):
    db, actor = context
    require_user(actor)
    rows = db.execute("""SELECT k.id,p.name,k.created_at,k.revoked_at FROM access_keys k
        JOIN principals p ON p.id=k.principal_id WHERE k.issued_by=%s AND p.kind='machine'
        ORDER BY k.created_at DESC,k.id""", (actor["id"],)).fetchall()
    for row in rows:
        row["folders"] = db.execute("""SELECT f.id,f.name FROM key_folder_scopes s
            JOIN folders f ON f.id=s.folder_id WHERE s.key_id=%s ORDER BY f.name,f.id""", (row["id"],)).fetchall()
        row["read_only"] = True
    return {"items": rows}


@app.delete("/v1/integrations/{key_id}")
def revoke_integration(key_id: UUID, context: Session):
    db, actor = context
    require_user(actor)
    row = db.execute("""SELECT k.id FROM access_keys k JOIN principals p ON p.id=k.principal_id
        WHERE k.id=%s AND k.issued_by=%s AND p.kind='machine'""", (key_id, actor["id"])).fetchone()
    if not row:
        raise HTTPException(404, "La integración no está disponible.")
    db.execute("UPDATE access_keys SET revoked_at=COALESCE(revoked_at,now()) WHERE id=%s", (key_id,))
    audit(db, actor["id"], "key.revoke", key_id)
    return {"revoked": True}


@app.post("/v1/folders", status_code=201)
def create_folder(body: Name, context: Session):
    db, actor = context
    require_user(actor)
    fid = uuid4()
    db.execute("INSERT INTO folders(id,owner_id,name) VALUES (%s,%s,%s)", (fid, actor["id"], body.name))
    audit(db, actor["id"], "folder.create", fid)
    return {"id": fid, "name": body.name, "private": True, "can_manage": True, "can_edit": True}


@app.get("/v1/folders")
def list_folders(context: Session):
    db, actor = context
    rows = db.execute("SELECT id FROM folders ORDER BY created_at,id").fetchall()
    result = []
    for row in rows:
        try:
            folder = authorize_folder(db, actor, row["id"])
            result.append({"id": folder["id"], "name": folder["name"],
                           "can_manage": folder["owner_id"] == actor["id"],
                           "can_edit": folder["owner_id"] == actor["id"] or folder["role"] == "editor"})
        except HTTPException:
            pass
    return {"items": result}


@app.get("/v1/folders/{folder_id}/members")
def folder_members(folder_id: UUID, context: Session):
    db, actor = context
    require_user(actor)
    folder = authorize_folder(db, actor, folder_id)
    if folder["owner_id"] != actor["id"]:
        raise HTTPException(403, "Solo el propietario puede consultar los accesos de la carpeta.")
    rows = db.execute("SELECT m.principal_id,p.name,m.role FROM folder_memberships m JOIN principals p ON p.id=m.principal_id WHERE m.folder_id=%s AND p.revoked_at IS NULL ORDER BY p.name,p.id", (folder_id,)).fetchall()
    return {"items": rows, "owner_id": folder["owner_id"]}


@app.put("/v1/folders/{folder_id}/members")
def share_folder(folder_id: UUID, body: Membership, context: Session):
    db, actor = context
    require_user(actor)
    folder = authorize_folder(db, actor, folder_id, True)
    if folder["owner_id"] != actor["id"]:
        raise HTTPException(403, "Solo el propietario puede compartir la carpeta.")
    principal = db.execute("SELECT kind FROM principals WHERE id=%s AND revoked_at IS NULL", (body.principal_id,)).fetchone()
    if not principal or principal["kind"] != "user":
        raise HTTPException(422, "Para integraciones, crea una clave con carpetas explícitas.")
    db.execute("INSERT INTO folder_memberships VALUES (%s,%s,%s) ON CONFLICT (folder_id,principal_id) DO UPDATE SET role=EXCLUDED.role", (folder_id, body.principal_id, body.role))
    audit(db, actor["id"], "folder.share", folder_id, {"principal_id": str(body.principal_id), "role": body.role})
    return {"shared": True}


@app.delete("/v1/folders/{folder_id}/members/{principal_id}")
def unshare_folder(folder_id: UUID, principal_id: UUID, context: Session):
    db, actor = context
    require_user(actor)
    folder = authorize_folder(db, actor, folder_id, True)
    if folder["owner_id"] != actor["id"]:
        raise HTTPException(403, "Solo el propietario puede revocar acceso.")
    db.execute("DELETE FROM folder_memberships WHERE folder_id=%s AND principal_id=%s", (folder_id, principal_id))
    audit(db, actor["id"], "folder.unshare", folder_id, {"principal_id": str(principal_id)})
    return {"revoked": True}


@app.post("/v1/meetings", status_code=201)
def create_meeting(body: Meeting, context: Session):
    db, actor = context
    require_user(actor)
    if body.folder_id:
        authorize_folder(db, actor, body.folder_id, True)
    request_hash = digest(json.dumps({"title": body.title,
        "folder_id": str(body.folder_id) if body.folder_id else None}, sort_keys=True))
    if body.idempotency_key is not None:
        # Serialize simultaneous delivery of the same capture, inside the same
        # transaction as its meeting and receipt. Actor scoping prevents leaks.
        lock = int(digest(str(actor["id"]) + ":" + body.idempotency_key)[:16], 16)
        if lock >= 2**63:
            lock -= 2**64
        db.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
        receipt = db.execute("SELECT request_hash,meeting_id FROM meeting_creation_receipts WHERE owner_id=%s AND idempotency_key=%s",
                             (actor["id"], body.idempotency_key)).fetchone()
        if receipt:
            if receipt["request_hash"] != request_hash:
                raise HTTPException(409, "La captura ya se recibió con otros datos.")
            authorize_meeting(db, actor, receipt["meeting_id"])
            return db.execute("SELECT id,state,revision FROM meetings WHERE id=%s", (receipt["meeting_id"],)).fetchone()
    mid = uuid4()
    db.execute("INSERT INTO meetings(id,owner_id,folder_id,title,state) VALUES (%s,%s,%s,%s,'capturing')", (mid, actor["id"], body.folder_id, body.title))
    if body.idempotency_key is not None:
        db.execute("INSERT INTO meeting_creation_receipts(owner_id,idempotency_key,request_hash,meeting_id) VALUES (%s,%s,%s,%s)",
                   (actor["id"], body.idempotency_key, request_hash, mid))
    audit(db, actor["id"], "meeting.create", mid)
    return {"id": mid, "state": "capturing", "revision": 1}


@app.get("/v1/meetings/{meeting_id}")
def get_meeting(meeting_id: UUID, context: Session):
    db, actor = context
    authorize_meeting(db, actor, meeting_id)
    return db.execute("SELECT id,folder_id,title,state,notes,revision,created_at,updated_at FROM meetings WHERE id=%s", (meeting_id,)).fetchone()


@app.patch("/v1/folders/{folder_id}")
def rename_folder(folder_id: UUID, body: Name, context: Session):
    db, actor = context
    require_user(actor)
    authorize_folder(db, actor, folder_id, True)
    db.execute("UPDATE folders SET name=%s WHERE id=%s", (body.name, folder_id))
    audit(db, actor["id"], "folder.rename", folder_id)
    return {"id": folder_id, "name": body.name}


@app.delete("/v1/folders/{folder_id}")
def delete_folder(folder_id: UUID, context: Session):
    db, actor = context
    require_user(actor)
    folder = authorize_folder(db, actor, folder_id, True)
    if folder["owner_id"] != actor["id"]:
        raise HTTPException(403, "Solo el propietario puede eliminar la carpeta.")
    db.execute("UPDATE meetings SET folder_id=NULL,revision=revision+1,updated_at=now() WHERE folder_id=%s", (folder_id,))
    db.execute("DELETE FROM folders WHERE id=%s", (folder_id,))
    audit(db, actor["id"], "folder.delete", folder_id)
    return {"deleted": True, "meetings_private": True}


@app.put("/v1/meetings/{meeting_id}/folder")
def assign_folder(meeting_id: UUID, body: Assignment, context: Session):
    db, actor = context
    require_user(actor)
    meeting = authorize_meeting(db, actor, meeting_id, True)
    if meeting["owner_id"] != actor["id"]:
        raise HTTPException(403, "Solo el propietario puede mover la reunión.")
    if body.folder_id:
        authorize_folder(db, actor, body.folder_id, True)
    db.execute("UPDATE meetings SET folder_id=%s,revision=revision+1,updated_at=now() WHERE id=%s", (body.folder_id, meeting_id))
    audit(db, actor["id"], "meeting.assign", meeting_id, {"folder_id": str(body.folder_id) if body.folder_id else None})
    return {"folder_id": body.folder_id}


@app.get("/v1/library")
def library(context: Session, after: UUID | None = None, limit: int = 25, folder_id: UUID | None = None):
    db, actor = context
    limit = min(max(limit, 1), 100)
    if folder_id: authorize_folder(db, actor, folder_id)
    # Keyset cursor is a UUID; inaccessible metadata is discarded before content reads.
    rows = db.execute("SELECT id FROM meetings WHERE (%s::uuid IS NULL OR id>%s) AND (%s::uuid IS NULL OR folder_id=%s) ORDER BY id", (after, after, folder_id, folder_id)).fetchall()
    result = []
    for row in rows:
        try:
            authorize_meeting(db, actor, row["id"])
        except HTTPException:
            continue
        result.append(db.execute("SELECT id,title,folder_id,state,revision,created_at,updated_at FROM meetings WHERE id=%s", (row["id"],)).fetchone())
        if len(result) == limit + 1:
            break
    return {"items": result[:limit], "next_cursor": result[limit-1]["id"] if len(result)>limit else None}


@app.put("/v1/meetings/{meeting_id}/notes")
def update_notes(meeting_id: UUID, body: Notes, context: Session):
    db, actor = context
    require_user(actor)
    authorize_meeting(db, actor, meeting_id, True)
    row = db.execute("UPDATE meetings SET notes=%s,revision=revision+1,updated_at=now() WHERE id=%s AND revision=%s RETURNING revision", (body.notes, meeting_id, body.expected_revision)).fetchone()
    if not row:
        raise HTTPException(409, "La reunión ha cambiado. Recarga antes de guardar.")
    audit(db, actor["id"], "notes.update", meeting_id, {"revision": row["revision"]})
    return row


@app.get("/v1/meetings/{meeting_id}/transcript")
def transcript(meeting_id: UUID, context: Session, version: int | None = None):
    db, actor = context
    authorize_meeting(db, actor, meeting_id)
    row = db.execute("SELECT version,content,content_hash,model_fingerprint FROM transcript_versions WHERE meeting_id=%s AND (%s::int IS NULL OR version=%s) ORDER BY version DESC LIMIT 1", (meeting_id, version, version)).fetchone()
    if not row:
        raise HTTPException(404, "Todavía no hay una transcripción disponible.")
    return row


@app.post("/v1/meetings/{meeting_id}/transcript", status_code=201)
def upload_transcript(meeting_id: UUID, body: TranscriptUpload, context: Session):
    db, actor = context
    require_user(actor)
    authorize_meeting(db, actor, meeting_id, True)
    meeting = db.execute("SELECT notes FROM meetings WHERE id=%s FOR UPDATE", (meeting_id,)).fetchone()
    content = body.transcript.model_dump()
    content_hash = digest(json.dumps(content, sort_keys=True, ensure_ascii=False))
    idem = str(meeting_id) + ":" + body.idempotency_key
    old = db.execute("SELECT id,payload FROM jobs WHERE idempotency_key=%s", (idem,)).fetchone()
    if old:
        if old["payload"].get("transcript_hash") != content_hash:
            raise HTTPException(409, "La misma clave identifica una transcripción distinta.")
        return {"job_id": old["id"], "transcript_version": old["payload"]["transcript_version"], "reused": True}
    version = db.execute("SELECT COALESCE(max(version),0)+1 AS version FROM transcript_versions WHERE meeting_id=%s", (meeting_id,)).fetchone()["version"]
    db.execute("INSERT INTO transcript_versions(meeting_id,version,content,content_hash,model_fingerprint,search_text) VALUES (%s,%s,%s,%s,%s,%s)",
               (meeting_id, version, Jsonb(content), content_hash, body.transcript.model_fingerprint, " ".join(s.text for s in body.transcript.segments)))
    jid, _ = submit(db, meeting_id, "document", idem, {"transcript_version": version, "transcript_hash": content_hash, "human_notes": meeting["notes"]})
    # Queued work for an earlier transcript is superseded; retain its history.
    # A running job remains fenced and cannot change the newer meeting's state.
    db.execute("""UPDATE jobs SET state='cancelled',updated_at=now()
        WHERE meeting_id=%s AND kind='document' AND state='queued'
        AND (payload->>'transcript_version')::int < %s""", (meeting_id, version))
    db.execute("UPDATE meetings SET state='queued',updated_at=now(),revision=revision+1 WHERE id=%s", (meeting_id,))
    audit(db, actor["id"], "transcript.upload", meeting_id, {"version": version, "job_id": str(jid)})
    return {"job_id": jid, "transcript_version": version, "reused": False}


@app.get("/v1/jobs/{job_id}")
def job_status(job_id: UUID, context: Session):
    db, actor = context
    metadata = db.execute("SELECT meeting_id FROM jobs WHERE id=%s", (job_id,)).fetchone()
    if not metadata:
        raise HTTPException(404, "El trabajo no está disponible.")
    authorize_meeting(db, actor, metadata["meeting_id"])
    return db.execute("""SELECT id,meeting_id,state,attempts,error_code,created_at,updated_at,
        (payload->>'transcript_version')::int AS transcript_version,
        (payload->>'transcript_version')::int =
        (SELECT max(version) FROM transcript_versions WHERE meeting_id=jobs.meeting_id) AS is_current
        FROM jobs WHERE id=%s""", (job_id,)).fetchone()


@app.get("/v1/meetings/{meeting_id}/jobs")
def meeting_jobs(meeting_id: UUID, context: Session):
    db,actor=context; authorize_meeting(db,actor,meeting_id)
    rows=db.execute("""SELECT id,meeting_id,state,attempts,error_code,created_at,updated_at,
        (payload->>'transcript_version')::int AS transcript_version,
        (payload->>'transcript_version')::int =
        (SELECT max(version) FROM transcript_versions WHERE meeting_id=jobs.meeting_id) AS is_current
        FROM jobs WHERE meeting_id=%s ORDER BY created_at DESC LIMIT 100""",(meeting_id,)).fetchall()
    return {"items":rows}


@app.post("/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: UUID, context: Session):
    db, actor = context
    require_user(actor)
    metadata = db.execute("SELECT meeting_id FROM jobs WHERE id=%s", (job_id,)).fetchone()
    if not metadata:
        raise HTTPException(404, "El trabajo no está disponible.")
    authorize_meeting(db, actor, metadata["meeting_id"], True)
    db.execute('SELECT id FROM meetings WHERE id=%s FOR UPDATE', (metadata['meeting_id'],))
    cancelled = cancel(db, job_id)
    if cancelled:
        audit(db, actor["id"], "job.cancel", job_id)
        db.execute("""UPDATE meetings SET state='cancelled',updated_at=now() WHERE id=%s
            AND (SELECT (payload->>'transcript_version')::int FROM jobs WHERE id=%s)=
            (SELECT max(version) FROM transcript_versions WHERE meeting_id=%s)""",
            (metadata['meeting_id'], job_id, metadata['meeting_id']))
    return {"cancelled": cancelled}


@app.post("/v1/jobs/{job_id}/retry")
def retry_job(job_id: UUID, context: Session):
    db, actor = context
    require_user(actor)
    row = db.execute("SELECT meeting_id,state,payload FROM jobs WHERE id=%s", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "El trabajo no está disponible.")
    authorize_meeting(db, actor, row["meeting_id"], True)
    db.execute("SELECT id FROM meetings WHERE id=%s FOR UPDATE", (row["meeting_id"],))
    row = db.execute("SELECT meeting_id,state,payload FROM jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
    latest = db.execute("SELECT max(version) AS version FROM transcript_versions WHERE meeting_id=%s", (row["meeting_id"],)).fetchone()["version"]
    if row["payload"].get("transcript_version") != latest:
        raise HTTPException(409, "Este trabajo pertenece a una transcripción anterior. Reintenta el análisis de la versión actual.")
    if row["state"] != "failed":
        raise HTTPException(409, "Solo se pueden reintentar trabajos fallidos.")
    db.execute("UPDATE jobs SET state='queued',error_code=NULL,available_at=now(),updated_at=now() WHERE id=%s", (job_id,))
    db.execute("UPDATE meetings SET state='queued',updated_at=now() WHERE id=%s", (row["meeting_id"],))
    audit(db, actor["id"], "job.retry", job_id)
    return {"queued": True}


@app.get("/v1/meetings/{meeting_id}/document")
def document(meeting_id: UUID, context: Session, version: int | None = None):
    db, actor = context
    authorize_meeting(db, actor, meeting_id)
    row = db.execute("""SELECT version,transcript_version,content,markdown,content_hash,model_fingerprint
        FROM document_versions WHERE meeting_id=%s AND
        ((%s::int IS NULL AND transcript_version=(SELECT max(version) FROM transcript_versions WHERE meeting_id=%s))
         OR version=%s) ORDER BY version DESC LIMIT 1""", (meeting_id, version, meeting_id, version)).fetchone()
    if not row:
        raise HTTPException(404, "Todavía no hay un acta para la transcripción actual.")
    return row


@app.get("/v1/search")
def search(q: str, context: Session, folder_id: UUID | None = None, limit: int = 10):
    db, actor = context
    if not q.strip() or len(q) > 500:
        raise HTTPException(422, "Introduce una búsqueda de hasta 500 caracteres.")
    if folder_id:
        authorize_folder(db, actor, folder_id)
    # Fetch candidate ids only. Read transcript content after authorization succeeds.
    rows = db.execute("""SELECT t.meeting_id,t.version,ts_rank(t.search_vector,plainto_tsquery('spanish',%s)) AS rank
        FROM transcript_versions t JOIN meetings m ON m.id=t.meeting_id
        WHERE t.search_vector @@ plainto_tsquery('spanish',%s)
        AND (%s::uuid IS NULL OR m.folder_id=%s)
        AND t.version=(SELECT max(t2.version) FROM transcript_versions t2 WHERE t2.meeting_id=t.meeting_id)
        ORDER BY rank DESC,t.meeting_id""", (q, q, folder_id, folder_id)).fetchall()
    result = []
    for row in rows:
        try:
            authorize_meeting(db, actor, row["meeting_id"])
        except HTTPException:
            continue
        saved = db.execute("SELECT content FROM transcript_versions WHERE meeting_id=%s AND version=%s", (row["meeting_id"], row["version"])).fetchone()["content"]
        segments = [s for s in saved["segments"] if q.casefold() in s["text"].casefold()][:3] or saved["segments"][:3]
        result.append({"meeting_id": row["meeting_id"], "transcript_version": row["version"],
                       "evidence": [{"segment_id": s["id"], "quote": s["text"]} for s in segments]})
        if len(result) >= min(max(limit, 1), 50):
            break
    return {"items": result}


@app.get("/v1/meetings/{meeting_id}/summary")
def summary(meeting_id: UUID, context: Session, version: int | None = None):
    return document(meeting_id,context,version)

@app.get("/v1/meetings/{meeting_id}/notes")
def read_notes(meeting_id: UUID, context: Session):
    meeting=get_meeting(meeting_id,context)
    return {"meeting_id":meeting_id,"notes":meeting["notes"],"revision":meeting["revision"]}

@app.get("/v1/meetings/{meeting_id}/versions")
def versions(meeting_id: UUID, context: Session):
    db,actor=context; authorize_meeting(db,actor,meeting_id)
    transcripts=db.execute("SELECT version,content_hash,model_fingerprint FROM transcript_versions WHERE meeting_id=%s ORDER BY version DESC",(meeting_id,)).fetchall()
    documents=db.execute("SELECT version,transcript_version,content_hash,model_fingerprint FROM document_versions WHERE meeting_id=%s ORDER BY version DESC",(meeting_id,)).fetchall()
    return {"meeting_id":meeting_id,"transcripts":transcripts,"documents":documents}


def tool(name, description, properties=None, required=None):
    return {"name":name,"description":description,"inputSchema":{"type":"object","properties":properties or {},"required":required or [],"additionalProperties":False},"annotations":{"readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False}}
MID={"meeting_id":{"type":"string","format":"uuid"}}
VERSION={"version":{"type":"integer","minimum":1}}
MCP_TOOLS=[
    tool("list_folders","Lista las carpetas autorizadas."),
    tool("list_meetings","Lista reuniones autorizadas, fechas, carpetas y estado.",{"after":{"type":"string","format":"uuid"},"limit":{"type":"integer","minimum":1,"maximum":100},"folder_id":{"type":"string","format":"uuid"}}),
    tool("get_meeting","Lee título, fecha, carpeta, notas y estado.",MID,["meeting_id"]),
    tool("get_transcript","Lee una transcripción versionada, sus segmentos y fuentes.",{**MID,**VERSION},["meeting_id"]),
    tool("get_document","Lee acta completa en JSON y Markdown con citas.",{**MID,**VERSION},["meeting_id"]),
    tool("get_summary","Lee resumen, decisiones, tareas y dudas, con citas.",{**MID,**VERSION},["meeting_id"]),
    tool("get_notes","Lee las notas humanas y su revisión.",MID,["meeting_id"]),
    tool("list_versions","Lista versiones y hashes de transcripciones y actas.",MID,["meeting_id"]),
    tool("get_processing_status","Consulta trabajos de una reunión o el estado de un trabajo autorizado.",{"job_id":{"type":"string","format":"uuid"},**MID}),
    tool("search_transcripts","Busca texto autorizado con referencias a segmentos.",{"q":{"type":"string","minLength":1,"maxLength":500},"folder_id":{"type":"string","format":"uuid"}},["q"]),
]

def validate_tool_args(name,args):
    schema=next((t['inputSchema'] for t in MCP_TOOLS if t['name']==name),None)
    if schema is None or not isinstance(args,dict) or set(args)-set(schema['properties']) or set(schema['required'])-set(args): raise ValueError('invalid_tool_arguments')
    if name=="get_processing_status" and len(args)!=1: raise ValueError("provide_job_or_meeting_id")
    for key,value in args.items():
        spec=schema['properties'][key]
        if spec['type']=='integer':
            if type(value)!=int or value<spec.get('minimum',-10**9) or value>spec.get('maximum',10**9): raise ValueError('invalid_tool_arguments')
        else:
            if not isinstance(value,str) or len(value)<spec.get('minLength',0) or len(value)>spec.get('maxLength',4000): raise ValueError('invalid_tool_arguments')
            if spec.get('format')=='uuid': UUID(value)

@app.get("/mcp")
@app.delete("/mcp")
def mcp_no_stream(context: Session):
    return Response(status_code=405,headers={"Allow":"POST, GET"})


@app.post("/mcp")
def mcp(body: RPC | list[RPC], context: Session, request: Request):
    if isinstance(body, list):
        if request.headers.get("MCP-Protocol-Version", "2025-03-26") != "2025-03-26":
            raise HTTPException(400,"MCP requiere un mensaje por petición.")
        if not body or len(body) > 100:
            raise HTTPException(400, "Lote MCP no válido.")
        results = [mcp(message, context, request) for message in body]
        results = [result for result in results if not isinstance(result, Response)]
        return results if results else Response(status_code=202)
    if body.id is None:
        return Response(status_code=202)
    result = None
    if body.method == "initialize":
        requested=body.params.get("protocolVersion","2025-03-26")
        negotiated=requested if requested in {"2025-03-26","2025-06-18","2025-11-25"} else "2025-11-25"
        result = {"protocolVersion": negotiated, "capabilities": {"tools": {}}, "serverInfo": {"name": "synth-meetings", "version": "0.1.0"}}
    elif body.method == "ping":
        result = {}
    elif body.method == "tools/list":
        result = {"tools": MCP_TOOLS}
    elif body.method == "tools/call":
        try:
            name, args = body.params["name"], body.params.get("arguments", {})
            validate_tool_args(name,args)
            if name == "list_meetings":
                result = library(context, UUID(args["after"]) if args.get("after") else None, int(args.get("limit", 25)), UUID(args["folder_id"]) if args.get("folder_id") else None)
            elif name == "list_folders": result=list_folders(context)
            elif name == "get_meeting": result=get_meeting(UUID(args["meeting_id"]),context)
            elif name == "get_notes": result=read_notes(UUID(args["meeting_id"]),context)
            elif name == "list_versions": result=versions(UUID(args["meeting_id"]),context)
            elif name == "get_processing_status": result=job_status(UUID(args["job_id"]),context) if "job_id" in args else meeting_jobs(UUID(args["meeting_id"]),context)
            elif name in ("get_transcript", "get_document", "get_summary"):
                result = (transcript if name == "get_transcript" else document)(UUID(args["meeting_id"]), context, int(args["version"]) if args.get("version") else None)
            elif name == "search_transcripts":
                result = search(str(args["q"]), context, UUID(args["folder_id"]) if args.get("folder_id") else None)
            else:
                raise ValueError("unknown_tool")
            result = {"content": [{"type": "text", "text": json.dumps(jsonable_encoder(result), ensure_ascii=False)}], "isError": False}
        except (HTTPException, KeyError, ValueError, TypeError):
            result = {"content": [{"type": "text", "text": "La herramienta o el recurso no están disponibles."}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": body.id, "error": {"code": -32601, "message": "Método no disponible."}}
    return {"jsonrpc": "2.0", "id": body.id, "result": result}
