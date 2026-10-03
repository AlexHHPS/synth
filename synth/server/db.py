"""Transactional persistence and authorization before content access."""
import hashlib
import os
import re
from pathlib import Path
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi import HTTPException
from .supabase_auth import auth_mode, authenticate_supabase, allowed_domains, configuration


def connect():
    if os.environ.get("DATABASE_URL"):
        schema = os.environ.get("DATABASE_SCHEMA", "public")
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            raise ValueError("database_schema_invalid")
        parameters = {"options": "-c search_path=" + schema}
        if os.environ.get("DATABASE_SSLROOTCERT"):
            parameters.update(sslmode="verify-full", sslrootcert=os.environ["DATABASE_SSLROOTCERT"])
        return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5, row_factory=dict_row, **parameters)
    password = Path(os.environ["POSTGRES_PASSWORD_FILE"]).read_text().strip()
    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        dbname="synth_meetings", user="synth", password=password,
        connect_timeout=5, row_factory=dict_row,
    )


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def audit(db, actor, action, resource, metadata=None):
    db.execute(
        "INSERT INTO audit_events(actor_id,action,resource_id,metadata) VALUES (%s,%s,%s,%s)",
        (actor, action, resource, Jsonb(metadata or {})),
    )


def migrate():
    mode = auth_mode()
    if mode == "supabase":
        configuration()
        domains = allowed_domains()
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(18280001)")
        db.execute(Path(__file__).with_name("schema.sql").read_text())
        if mode == "supabase":
            db.execute("CREATE TABLE IF NOT EXISTS allowed_auth_domains (domain text PRIMARY KEY)")
            # Dedicated Supabase project only: keep the registration hook and
            # runtime API checks on the same env-driven domain policy.
            db.execute("DELETE FROM allowed_auth_domains WHERE NOT (domain=ANY(%s))", (domains,))
            for domain in domains:
                db.execute("INSERT INTO allowed_auth_domains(domain) VALUES (%s) ON CONFLICT DO NOTHING", (domain,))
        bootstrap_file = os.environ.get("BOOTSTRAP_ADMIN_KEY_FILE")
        bootstrap = Path(bootstrap_file).read_text().strip() if bootstrap_file else os.environ["BOOTSTRAP_ADMIN_KEY"]
        admin = uuid.UUID("00000000-0000-4000-8000-000000000001")
        db.execute(
            "INSERT INTO principals(id,name,kind) VALUES (%s,'Administrador local','user') ON CONFLICT DO NOTHING",
            (admin,),
        )
        db.execute(
            "INSERT INTO access_keys(id,principal_id,token_hash) VALUES (%s,%s,%s) ON CONFLICT (token_hash) DO NOTHING",
            (uuid.uuid4(), admin, digest(bootstrap)),
        )


def authenticate(db, token):
    if not token:
        raise HTTPException(401, "Se necesita una clave de acceso.")
    actor = db.execute(
        """SELECT p.id,p.name,p.kind,k.id AS key_id FROM access_keys k
        JOIN principals p ON p.id=k.principal_id
        WHERE k.token_hash=%s AND k.revoked_at IS NULL AND p.revoked_at IS NULL""",
        (digest(token),),
    ).fetchone()
    mode = auth_mode()
    if actor and (mode == "keys" or actor["kind"] == "machine"):
        return actor
    if mode == "supabase":
        if actor:
            # Human legacy keys cannot bypass the corporate domain check.
            raise HTTPException(401, "Inicia sesión con tu cuenta corporativa.")
        return authenticate_supabase(db, token)
    if not actor:
        raise HTTPException(401, "La clave no es válida o ha sido revocada.")
    return actor


def authorize_folder(db, actor, folder_id, write=False):
    # Always fetch metadata only before fetching meeting/transcript content.
    row = db.execute(
        """SELECT f.id,f.owner_id,f.name,m.role FROM folders f
        LEFT JOIN folder_memberships m ON m.folder_id=f.id AND m.principal_id=%s
        WHERE f.id=%s""", (actor["id"], folder_id),
    ).fetchone()
    allowed = row and (row["owner_id"] == actor["id"] or row["role"] in (("editor",) if write else ("reader", "editor")))
    if actor["kind"] == "machine":
        allowed = allowed and not write and db.execute(
            "SELECT 1 FROM key_folder_scopes WHERE key_id=%s AND folder_id=%s",
            (actor["key_id"], folder_id),
        ).fetchone()
    if not allowed:
        raise HTTPException(404, "La carpeta no está disponible.")
    return row


def authorize_meeting(db, actor, meeting_id, write=False):
    row = db.execute("SELECT id,owner_id,folder_id FROM meetings WHERE id=%s", (meeting_id,)).fetchone()
    if not row:
        raise HTTPException(404, "La reunión no está disponible.")
    if row["owner_id"] == actor["id"] and actor["kind"] == "user":
        return row
    if not row["folder_id"]:
        raise HTTPException(404, "La reunión no está disponible.")
    authorize_folder(db, actor, row["folder_id"], write)
    return row
