"""Server-side Supabase identity checks; no trust in client-provided claims.

Only public/publishable Auth keys are needed. Folder authorization remains in
Synth. Fail closed on an unavailable Auth service or invalid domain policy.
"""
from datetime import datetime, timezone
import json
import base64
import os
import re
import ssl
import urllib.error
import urllib.request
from uuid import UUID, uuid4

from fastapi import HTTPException


def auth_mode():
    mode = os.environ.get("AUTH_MODE", "keys")
    if mode not in {"keys", "supabase"}:
        raise HTTPException(503, "La autenticación no está configurada correctamente.")
    return mode


def allowed_domains():
    domains = {value.strip().lower() for value in os.environ.get("ALLOWED_DOMAINS", "").split(",") if value.strip()}
    if not domains or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", d) or "." not in d or ".." in d for d in domains):
        raise HTTPException(503, "La política de dominios no es válida.")
    return sorted(domains)


def permitted_email(email):
    if not isinstance(email, str) or email != email.strip() or email.count("@") != 1:
        return False
    local, domain = email.rsplit("@", 1)
    return bool(local) and not any(c.isspace() or ord(c) < 32 for c in email) and domain.lower() in allowed_domains()


def configuration():
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    # A hosted project is selected by operators, never by an incoming request.
    public_key = key.startswith("sb_publishable_")
    if not public_key:
        try:
            payload = key.split(".")[1]
            public_key = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["role"] == "anon"
        except (ValueError, IndexError, KeyError, TypeError):
            pass
    if not re.fullmatch(r"https://[a-z0-9]+\.supabase\.co", url) or not public_key:
        raise HTTPException(503, "Falta configurar Supabase Auth.")
    return url, key


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def verified_user(token):
    url, key = configuration()
    if not isinstance(token, str) or not 1 <= len(token) <= 16384 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise HTTPException(401, "La sesión no es válida.")
    request = urllib.request.Request(url + "/auth/v1/user", headers={"apikey": key, "Authorization": "Bearer " + token})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    try:
        with opener.open(request, timeout=8) as response:
            content = response.read(65537)
            if len(content) > 65536:
                raise ValueError("auth_response_limit")
            user = json.loads(content)
    except urllib.error.HTTPError as error:
        if error.code in {400, 401, 403, 404}:
            raise HTTPException(401, "La sesión no es válida o ha caducado.") from None
        raise HTTPException(503, "El servicio de autenticación no está disponible.") from None
    except (OSError, ValueError, TimeoutError):
        raise HTTPException(503, "El servicio de autenticación no está disponible.") from None
    return validate_user(user)


def validate_user(user):
    if not isinstance(user, dict):
        raise HTTPException(401, "La sesión no es válida.")
    try:
        user_id = UUID(user["id"])
        confirmed = datetime.fromisoformat(user["email_confirmed_at"].replace("Z", "+00:00"))
        if confirmed.tzinfo is None or confirmed > datetime.now(timezone.utc):
            raise ValueError("email_unconfirmed")
        banned = user.get("banned_until")
        if banned and datetime.fromisoformat(banned.replace("Z", "+00:00")) > datetime.now(timezone.utc):
            raise ValueError("user_banned")
        if user.get("is_anonymous") is not False or user.get("deleted_at"):
            raise ValueError("user_unavailable")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HTTPException(401, "Se necesita una cuenta verificada y activa.") from None
    if not permitted_email(user.get("email")):
        raise HTTPException(403, "Esta cuenta no pertenece a un dominio autorizado.")
    return {"id": user_id, "email": user["email"].lower()}


def authenticate_supabase(db, token):
    identity = verified_user(token)
    verify_live_session(db,identity,token)
    # An explicit mapping preserves old meeting/profile owners. Never match an
    # old principal by name or by unverified user_metadata supplied at signup.
    db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(identity["id"]),))
    row = db.execute("""SELECT p.id,p.name,p.kind,p.revoked_at FROM auth_identities i
        JOIN principals p ON p.id=i.principal_id WHERE i.provider='supabase'
        AND i.subject=%s""", (identity["id"],)).fetchone()
    if row is None:
        principal = uuid4()
        db.execute("INSERT INTO principals(id,name,kind) VALUES (%s,%s,'user')", (principal, identity["email"]))
        db.execute("INSERT INTO auth_identities(provider,subject,principal_id) VALUES ('supabase',%s,%s)", (identity["id"], principal))
        row = {"id": principal, "name": identity["email"], "kind": "user", "revoked_at": None}
    if row["revoked_at"] is not None or row["kind"] != "user" or str(row["id"]) == "00000000-0000-4000-8000-000000000001":
        raise HTTPException(403, "El acceso de esta cuenta ha sido revocado.")
    if row["name"].startswith("Operador local"):
        row["name"] = identity["email"]
        db.execute("UPDATE principals SET name=%s WHERE id=%s", (row["name"], row["id"]))
    return {"id": row["id"], "name": row["name"], "kind": "user", "key_id": None,
            "auth_provider": "supabase", "email": identity["email"]}


def verify_live_session(db, identity, token):
    if os.environ.get("SUPABASE_CHECK_SESSIONS") != "1":
        return
    # The JWT has already been validated by this project's Auth server. This
    # extra DB lookup makes logout/session deletion effective immediately.
    try:
        payload=token.split(".")[1]
        claims=json.loads(base64.urlsafe_b64decode(payload+"="*(-len(payload)%4)))
        session_id=UUID(claims["session_id"])
        if UUID(claims["sub"])!=identity["id"]: raise ValueError("subject_mismatch")
    except (ValueError,KeyError,IndexError,TypeError):
        raise HTTPException(401,"La sesión no es válida.") from None
    row=db.execute("SELECT has_auth_session(%s,%s) AS active",(session_id,identity["id"])).fetchone()
    if not row or not row["active"]:
        raise HTTPException(401,"La sesión ha sido cerrada.")
