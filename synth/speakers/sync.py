"""Sync corporate centroids over authenticated HTTPS; never recordings or JS data."""
from contextlib import closing
import hashlib
import json
from uuid import UUID
from .profiles import normalized


def local_profiles(actor):
    from .onboarding import vault
    with closing(vault()) as v:
        rows = v.db.execute("SELECT * FROM profiles WHERE employee_id=? AND revoked=0", (actor["id"],)).fetchall()
        return [{"id": r["id"], "employee_id": str(actor["id"]), "name": actor["name"],
                 "model": r["model"], "updated_at": r["created_at"],
                 "shared": False,
                 "vector": normalized(json.loads(v.key.decrypt(r["nonce"], r["ciphertext"],
                                       (r["id"] + ":" + r["model"]).encode())))} for r in rows]


def catalog(actor):
    from synth.worker.desktop_pipeline import LocalAPI
    profiles = local_profiles(actor)
    state = "local_only"
    api = LocalAPI()
    if api.auth_mode == "supabase":
        try:
            remote = api.call("GET", "/v1/voice/profiles")
            profiles = [{"id": str(UUID(p["id"])), "employee_id": str(UUID(p["employee_id"])),
                         "name": p["name"], "model": p["model"], "updated_at": p["updated_at"],
                         "shared": True,
                         "vector": normalized(p["embedding"])} for p in remote["profiles"]]
            ids = {p["id"] for p in profiles}
            profiles += [p for p in local_profiles(actor) if p["id"] not in ids]
            state = "synced"
        except (ValueError, OSError, KeyError):
            # Do not keep coworker vectors after a network failure or session revocation.
            state = "sync_unavailable"
    metadata = [{k: p[k] for k in ("id", "employee_id", "model", "updated_at")} for p in profiles]
    signature = hashlib.sha256(json.dumps(metadata, sort_keys=True, default=str).encode()).hexdigest()
    return profiles, signature, state


def share(actor, profile_id, consent):
    from synth.worker.desktop_pipeline import LocalAPI
    if consent is not True:
        raise ValueError("voice_share_consent_required")
    profile_id = str(UUID(profile_id))
    profile = next((p for p in local_profiles(actor) if p["id"] == profile_id), None)
    if profile is None:
        raise ValueError("profile_missing")
    return LocalAPI().call("PUT", "/v1/voice/profiles", {"id": profile_id,
        "model": profile["model"], "embedding": profile["vector"], "consent_confirmed": True})


def unshare(profile_id):
    from synth.worker.desktop_pipeline import LocalAPI
    api = LocalAPI()
    if api.auth_mode != "supabase":
        return
    try:
        api.call("DELETE", "/v1/voice/profiles/" + str(UUID(profile_id)))
    except ValueError as error:
        if str(error) != "api_http_404":
            raise
