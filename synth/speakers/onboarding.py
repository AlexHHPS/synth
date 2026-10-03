"""Local enrollment and exploratory comparison; never asserts employee identity.
Raw enrollment/test samples are erased on success or failure. Real calibration
is a separate gate and cannot be inferred from one employee's onboarding.
"""
from array import array
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
import wave
from uuid import UUID, uuid4
from .native import infer, LOCK
from .profiles import Vault, normalized
from synth.worker.desktop_pipeline import APPDATA, STATE, inference_lock, save_json
from synth.worker.asr import normalize_audio
from synth.worker.journal import file_hash

ROOT = APPDATA / "voice"
RAW = ROOT / "onboarding/raw"
JOBS = ROOT / "jobs"
_GATE = threading.Lock()

def vault():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    from .keychain import obtain
    key = obtain(ROOT / "vault-key")
    return Vault(ROOT / "profiles.sqlite3", key)

def status(actor):
    with closing(vault()) as v:
        rows = v.db.execute("SELECT id,created_at,revoked,model FROM profiles WHERE employee_id=? ORDER BY created_at DESC", (actor["id"],)).fetchall()
    from .sync import catalog
    profiles, _, sync_state = catalog(actor)
    return {"employee_name": actor["name"], "profiles": [dict(r) for r in rows],
        "corporate_profiles": [{"id": p["id"], "name": p["name"], "employee_id": p["employee_id"]} for p in profiles if p["shared"]],
        "sync_state": sync_state, "key_storage": "macos_keychain",
        "identification_state": "calibration_pending", "audio_location": "this_mac"}

def delete(actor, profile_id):
    profile_id = str(UUID(profile_id))
    if not _GATE.acquire(blocking=False): raise ValueError("voice_busy")
    try:
        with closing(vault()) as v:
            if not v.db.execute("SELECT 1 FROM profiles WHERE id=? AND employee_id=?", (profile_id, actor["id"])).fetchone(): raise ValueError("profile_missing")
            from .sync import unshare
            unshare(profile_id)
            v.delete(profile_id)
    finally: _GATE.release()
    return {"deleted": True}

def read_job(actor, identifier):
    identifier = str(UUID(identifier))
    path = JOBS / (identifier + ".json")
    if not path.is_file(): raise ValueError("voice_job_missing")
    value = json.loads(path.read_text())
    if value.get("employee_id") != actor["id"]: raise ValueError("voice_job_missing")
    return {k: value[k] for k in ("id", "state", "operation", "created_at", "error_code", "result")}

def samples(path):
    with wave.open(str(path)) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2: raise ValueError("voice_audio_invalid")
        out = array("h", w.readframes(w.getnframes()))
    return out

def clean_samples(audio, turns, speaker):
    selected = array("h")
    end = 0
    for turn in sorted(turns, key=lambda t: t["start_ms"]):
        if turn["speaker_id"] != speaker: continue
        start, stop = max(end, round(turn["start_ms"] * 16)), round(turn["end_ms"] * 16)
        if stop > start: selected.extend(audio[start:stop]); end = stop
    return selected

def quality(audio, turns):
    if not audio: raise ValueError("enrollment_low_quality")
    overlap = any(a["end_ms"] > b["start_ms"] for a,b in zip(sorted(turns,key=lambda t:t["start_ms"]), sorted(turns,key=lambda t:t["start_ms"])[1:]))
    rms = math.sqrt(sum(float(s)*s for s in audio)/len(audio))/32768
    return {"clean_speech_seconds": len(audio)/16000, "clipped_fraction": sum(abs(s)>=32700 for s in audio)/len(audio), "rms_dbfs": 20*math.log10(max(rms,1e-10)), "overlap": overlap}

def embed_chunk(audio, directory, index, lock_fd):
    path = directory / (str(index)+".wav")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(audio.tobytes())
    try: return infer("embed",path,directory,lock_fd=lock_fd)["embedding"]
    finally: path.unlink(missing_ok=True)

def submit(actor, recording_id, consent, operation):
    if consent is not True or operation not in {"enroll","test"}: raise ValueError("voice_consent_required")
    recording_id = str(UUID(recording_id))
    RAW.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = RAW / (recording_id+".wav")
    if path.is_symlink() or not path.is_file() or path.resolve().parent != RAW.resolve() or not 44 < path.stat().st_size <= 24*1024*1024: raise ValueError("voice_audio_invalid")
    with wave.open(str(path)) as w:
        if w.getnchannels()!=1 or w.getsampwidth()!=2 or not 15 <= w.getnframes()/w.getframerate() <= 35: raise ValueError("voice_audio_invalid")
    if not _GATE.acquire(blocking=False): raise ValueError("voice_busy")
    identifier=str(uuid4()); JOBS.mkdir(parents=True,exist_ok=True,mode=0o700)
    job={"id":identifier,"employee_id":actor["id"],"operation":operation,"recording_id":recording_id,"state":"running","created_at":time.time(),"error_code":None,"result":None}
    try: save_json(JOBS/(identifier+".json"),job)
    except BaseException: _GATE.release(); raise
    def run():
        import tempfile
        try:
            with inference_lock(STATE) as lock_fd, tempfile.TemporaryDirectory(prefix="voice-",dir=RAW) as temporary:
                directory=Path(temporary); normalized_path=directory/"normalized.wav"
                normalize_audio(path,normalized_path,lock_fd=lock_fd)
                d=infer("diarize",normalized_path,directory,lock_fd=lock_fd)
                turns=d["segments"]; speakers=sorted({t["speaker_id"] for t in turns}); audio=samples(normalized_path)
                model=json.loads(LOCK.read_text())["model_revision"]
                if operation=="enroll":
                    if len(speakers)!=1: raise ValueError("enrollment_requires_one_voice")
                    clean=clean_samples(audio,turns,speakers[0]); q=quality(clean,turns)
                    if q["clean_speech_seconds"]<15 or q["clipped_fraction"]>.01 or q["rms_dbfs"] < -40 or q["overlap"]: raise ValueError("enrollment_low_quality")
                    vectors=[embed_chunk(clean[i*80000:(i+1)*80000],directory,i,lock_fd) for i in range(3)]
                    with closing(vault()) as v:
                        # Replacement does not silently erase earlier enrollment provenance.
                        pid=v.enroll(actor["id"],model,vectors,identifier,[file_hash(path)],q)
                    result={"profile_id":pid,"quality":q,"speakers_detected":1,"identification_state":"calibration_pending","audio_deleted":True}
                else:
                    comparisons=[]
                    with closing(vault()) as v:
                        rows=v.db.execute("SELECT * FROM profiles WHERE employee_id=? AND revoked=0 AND model=?",(actor["id"],model)).fetchall()
                        if not rows: raise ValueError("no_active_profiles")
                        for speaker in speakers:
                            clean=clean_samples(audio,turns,speaker)
                            if len(clean)<48000:
                                comparisons.append({"speaker_id":speaker,"clean_seconds":len(clean)/16000,"similarity":None,"reason":"short_or_unclean"}); continue
                            vector=embed_chunk(clean[:160000],directory,speaker,lock_fd)
                            scores=[]
                            for row in rows:
                                stored=normalized(json.loads(v.key.decrypt(row["nonce"],row["ciphertext"],(row["id"]+":"+model).encode())))
                                scores.append(sum(a*b for a,b in zip(vector,stored)))
                            comparisons.append({"speaker_id":speaker,"clean_seconds":len(clean)/16000,"similarity":round(max(scores),4),"reason":"exploratory_uncalibrated"})
                    result={"speakers_detected":len(speakers),"turns":turns,"comparisons":comparisons,"identification_state":"calibration_pending","audio_deleted":True}
            path.unlink(missing_ok=True)
            job.update(state="succeeded",result=result)
        except Exception as e:
            code=str(e)
            job.update(state="failed",error_code=code if code.replace('_','').isalnum() and len(code)<100 else "voice_processing_failed")
        finally:
            try: path.unlink(missing_ok=True)
            except OSError: job.update(state="failed",error_code="voice_audio_cleanup_failed",result=None)
            try: save_json(JOBS/(identifier+".json"),job)
            finally: _GATE.release()
    threading.Thread(target=run,daemon=True).start()
    return {"id":identifier,"state":"running"}


def recover_interrupted_jobs():
    if not JOBS.exists(): return
    for path in JOBS.glob("*.json"):
        if path.is_symlink(): continue
        job=json.loads(path.read_text())
        if job.get("state") != "running": continue
        if job.get("recording_id"):
            recording_id=str(UUID(job["recording_id"]))
            (RAW/(recording_id+".wav")).unlink(missing_ok=True)
        job.update(state="failed",error_code="voice_processing_interrupted",result=None)
        save_json(path,job)
