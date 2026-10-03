"""Google PKCE in the system browser, with session secrets kept in Mac Keychain.

Called only by the authenticated loopback host. No token crosses the JS bridge.
"""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

APPDATA = Path.home() / "Library/Application Support/dev.synth.voice"
SERVICE = "dev.synth.voice.supabase"
from synth.config import API_URL as API, SUPABASE_URL as SUPABASE
ACCOUNT = SUPABASE or API
FLOW = None
GATE = threading.RLock()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def fetch(url, method="GET", body=None, headers=None):
    if not url.startswith((API + "/", SUPABASE + "/")):
        raise ValueError("auth_destination_denied")
    context = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(url, method=method, headers={"Content-Type":"application/json", **(headers or {})},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with opener.open(request, timeout=20) as response:
            data = response.read(131073)
            if len(data)>131072: raise ValueError("auth_response_limit")
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as error:
        raise ValueError("auth_http_" + str(error.code)) from None
    except (OSError, ValueError) as error:
        if isinstance(error, ValueError) and str(error)=="auth_response_limit": raise
        raise ValueError("auth_unavailable") from None


def configuration():
    value = fetch(API + "/v1/auth/config")
    if value.get("mode") != "supabase": return value
    if value.get("supabase_url") != SUPABASE or not value.get("publishable_key"):
        raise ValueError("auth_configuration_invalid")
    return value


@contextmanager
def session_lock():
    with GATE:
        fd=os.open(APPDATA/"supabase-session.lock",os.O_CREAT|os.O_RDWR,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX); yield
        finally: os.close(fd)


def read_session():
    result=subprocess.run(["/usr/bin/security","find-generic-password","-s",SERVICE,"-a",ACCOUNT,"-w"],capture_output=True,text=True,timeout=10)
    if result.returncode: return None
    try: return json.loads(base64.b64decode(result.stdout.strip()))
    except (ValueError,TypeError): raise ValueError("auth_keychain_invalid") from None


def store_session(value):
    # Feed secrets through stdin, never process arguments or shell interpolation.
    payload=base64.b64encode(json.dumps(value,separators=(",",":")).encode()).decode()
    command="add-generic-password -U -s " + shlex.quote(SERVICE) + " -a " + shlex.quote(ACCOUNT) + " -w " + shlex.quote(payload) + "\n"
    result=subprocess.run(["/usr/bin/security","-i"],input=command,capture_output=True,text=True,timeout=15)
    if result.returncode: raise ValueError("auth_keychain_write_failed")


def accept_tokens(value, config, previous=None):
    if not isinstance(value.get("access_token"),str) or not isinstance(value.get("refresh_token"),str):
        raise ValueError("auth_session_invalid")
    headers={"Authorization":"Bearer "+value["access_token"]}
    if previous:
        actor=fetch(API+"/v1/me",headers=headers)
        if actor["id"] != previous["principal_id"]: raise ValueError("auth_identity_changed")
    else:
        legacy=(APPDATA/"desktop-api-key").read_text().strip()
        try:
            actor=fetch(API+"/v1/auth/link","POST",{"legacy_key":legacy},headers)
        except ValueError as error:
            if str(error)!="auth_http_401": raise
            actor=fetch(API+"/v1/me",headers=headers)
    if actor.get("kind")!="user": raise ValueError("auth_identity_invalid")
    session={"access_token":value["access_token"],"refresh_token":value["refresh_token"],
        "expires_at":time.time()+int(value.get("expires_in",3600)),"principal_id":actor["id"],
        "email":actor.get("email") or value.get("user",{}).get("email"),"publishable_key":config["publishable_key"]}
    store_session(session)
    return session


def access():
    with session_lock():
        session=read_session()
        if session is None: raise ValueError("auth_login_required")
        if session["expires_at"] < time.time()+90:
            value=fetch(SUPABASE+"/auth/v1/token?grant_type=refresh_token","POST",{"refresh_token":session["refresh_token"]},
                {"apikey":session["publishable_key"]})
            session=accept_tokens(value,{"publishable_key":session["publishable_key"]},session)
        return session


def state():
    config=configuration()
    if config.get("mode")!="supabase": return {"mode":"keys","signed_in":True}
    with session_lock(): session=read_session()
    return {"mode":"supabase","signed_in":session is not None,"email":session.get("email") if session else None,
        "allowed_domains":config["allowed_domains"],"flow_state":FLOW.get("state") if FLOW else None,
        "error_code":FLOW.get("error_code") if FLOW else None}


def begin():
    global FLOW
    config=configuration()
    if config.get("mode")!="supabase": raise ValueError("auth_not_enabled")
    with GATE:
        verifier=secrets.token_urlsafe(48); nonce=secrets.token_urlsafe(32)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        redirect="http://127.0.0.1:18383/auth/callback/"+nonce
        FLOW={"nonce":nonce,"verifier":verifier,"expires_at":time.time()+300,"state":"waiting","config":config}
        url=SUPABASE+"/auth/v1/authorize?"+urllib.parse.urlencode({"provider":"google","redirect_to":redirect,
            "code_challenge":challenge,"code_challenge_method":"s256","scopes":"openid email profile","prompt":"select_account"})
        subprocess.run(["/usr/bin/open",url],check=True,capture_output=True,timeout=10)
        return {"state":"waiting"}


def callback(path):
    global FLOW
    parsed=urllib.parse.urlsplit(path); query=urllib.parse.parse_qs(parsed.query)
    with GATE:
        flow=FLOW
        if not flow or flow["state"]!="waiting" or flow["expires_at"]<time.time() or parsed.path!="/auth/callback/"+flow["nonce"]:
            raise ValueError("auth_callback_invalid")
        flow["state"]="completing"
    try:
        if "error" in query or len(query.get("code",[]))!=1: raise ValueError("auth_google_denied")
        value=fetch(SUPABASE+"/auth/v1/token?grant_type=pkce","POST",
            {"auth_code":query["code"][0],"code_verifier":flow["verifier"]},{"apikey":flow["config"]["publishable_key"]})
        with session_lock(): accept_tokens(value,flow["config"])
        flow.update(state="signed_in",error_code=None)
    except ValueError as error:
        flow.update(state="failed",error_code=str(error)); raise
    finally:
        flow.pop("verifier",None); flow.pop("nonce",None); flow.pop("config",None)


def logout():
    global FLOW
    with session_lock():
        session=read_session()
        if session:
            try: fetch(SUPABASE+"/auth/v1/logout?scope=local","POST",{}, {"apikey":session["publishable_key"],"Authorization":"Bearer "+session["access_token"]})
            finally:
                result=subprocess.run(["/usr/bin/security","delete-generic-password","-s",SERVICE,"-a",ACCOUNT],capture_output=True,timeout=10)
                if result.returncode: raise ValueError("auth_keychain_delete_failed")
        FLOW=None
    return {"signed_in":False}
