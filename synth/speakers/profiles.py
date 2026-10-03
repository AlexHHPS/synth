"""Encrypted profile lifecycle and calibrated, abstaining cosine matching."""
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def normalized(vector):
    if len(vector) != 256 or any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) for x in vector):
        raise ValueError("invalid_embedding")
    norm = math.sqrt(sum(x*x for x in vector))
    if not math.isfinite(norm) or norm < 1e-8:
        raise ValueError("empty_embedding")
    return [x/norm for x in vector]


class Vault:
    def __init__(self, path, key_file):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.key = AESGCM(key_file if isinstance(key_file, bytes) else bytes.fromhex(Path(key_file).read_text().strip()))
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY, employee_id TEXT NOT NULL, model TEXT NOT NULL,
                nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, consent_id TEXT NOT NULL,
                source_hashes TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY, action TEXT NOT NULL,
                profile_id TEXT NOT NULL, consent_id TEXT, created_at REAL NOT NULL
            );
        """)

    def close(self):
        self.db.close()

    def enroll(self, employee_id, model, vectors, consent_id, source_hashes, quality):
        if not employee_id or not model or not consent_id or len(set(source_hashes)) < 1:
            raise ValueError("enrollment_provenance_missing")
        if (len(vectors) < 3 or quality.get("clean_speech_seconds", 0) < 15 or
                quality.get("clipped_fraction", 1) > 0.01 or quality.get("rms_dbfs", -100) < -40 or
                quality.get("overlap", True)):
            raise ValueError("enrollment_low_quality")
        vectors = [normalized(v) for v in vectors]
        centroid = normalized([sum(v[i] for v in vectors)/len(vectors) for i in range(256)])
        consistency = min(sum(a*b for a, b in zip(v, centroid)) for v in vectors)
        if consistency < 0.65:
            raise ValueError("enrollment_inconsistent_speaker")
        pid, nonce = str(uuid4()), os.urandom(12)
        ciphertext = self.key.encrypt(nonce, json.dumps(centroid).encode(), (pid+":"+model).encode())
        with self.db:
            self.db.execute("INSERT INTO profiles(id,employee_id,model,nonce,ciphertext,consent_id,source_hashes,created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (pid, employee_id, model, nonce, ciphertext, consent_id, json.dumps(source_hashes), time.time()))
            self.db.execute("INSERT INTO events(action,profile_id,consent_id,created_at) VALUES ('enroll',?,?,?)", (pid, consent_id, time.time()))
        return pid

    def revoke(self, profile_id):
        with self.db:
            if not self.db.execute("UPDATE profiles SET revoked=1 WHERE id=?", (profile_id,)).rowcount:
                raise ValueError("profile_missing")
            self.db.execute("INSERT INTO events(action,profile_id,created_at) VALUES ('revoke',?,?)", (profile_id, time.time()))

    def delete(self, profile_id):
        with self.db:
            if not self.db.execute("DELETE FROM profiles WHERE id=?", (profile_id,)).rowcount:
                raise ValueError("profile_missing")
            self.db.execute("INSERT INTO events(action,profile_id,created_at) VALUES ('delete',?,?)", (profile_id, time.time()))
        self.db.execute("VACUUM")

    def match(self, vector, model, calibration, overlap=False, clean_seconds=0):
        unknown = {"employee_id": None, "reason": "unknown", "score": None, "margin": None}
        if overlap or clean_seconds < 3:
            return {**unknown, "reason": "overlap" if overlap else "short_or_unclean"}
        if calibration.get("state") != "FROZEN" or calibration.get("model") != model:
            return {**unknown, "reason": "calibration_unavailable"}
        threshold, margin_min = calibration.get("threshold"), calibration.get("margin")
        if (not isinstance(threshold, (float, int)) or not -1 <= threshold <= 1 or
                not isinstance(margin_min, (float, int)) or not 0 <= margin_min <= 2 or
                not calibration.get("dataset_hash") or not calibration.get("id")):
            raise ValueError("invalid_calibration")
        vector = normalized(vector)
        scores = {}
        for profile in self.db.execute("SELECT * FROM profiles WHERE revoked=0 AND model=?", (model,)):
            stored = json.loads(self.key.decrypt(profile["nonce"], profile["ciphertext"], (profile["id"]+":"+model).encode()))
            score = sum(a*b for a, b in zip(vector, normalized(stored)))
            scores[profile["employee_id"]] = max(scores.get(profile["employee_id"], -1), score)
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        if not ranked:
            return {**unknown, "reason": "no_active_profiles"}
        employee, score = ranked[0]
        margin = score - (ranked[1][1] if len(ranked)>1 else -1)
        if score < threshold or margin < margin_min:
            return {**unknown, "reason": "below_threshold" if score < threshold else "ambiguous", "score": score, "margin": margin}
        return {"employee_id": employee, "score": score, "margin": margin,
                "reason": "matched", "calibration_id": calibration["id"]}
