"""Per-meeting speaker voiceprints: encrypted on this Mac, never exported.

One row per (meeting, cluster): the cluster's embedding centroid, so a meeting
can be re-identified after a profile changes without keeping any audio. Rows
are erased when the meeting's speakers are confirmed or dismissed, and at the
latest RETENTION_SECONDS after they were stored.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .profiles import normalized

RETENTION_SECONDS = 30 * 24 * 3600


class MeetingPrints:
    def __init__(self, path: Path, key: bytes, clock: Callable[[], float] = time.time):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("""CREATE TABLE IF NOT EXISTS prints (
            task_id TEXT NOT NULL, cluster TEXT NOT NULL, model TEXT NOT NULL,
            nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, clean_seconds REAL NOT NULL,
            created_at REAL NOT NULL, PRIMARY KEY (task_id, cluster))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS window_prints (
            task_id TEXT NOT NULL, cluster TEXT NOT NULL, model TEXT NOT NULL,
            nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY (task_id, cluster, model))""")
        self.key, self.clock = AESGCM(key), clock

    def close(self) -> None:
        self.db.close()

    def _aad(self, task_id: str, cluster: str, model: str) -> bytes:
        return f"{task_id}:{cluster}:{model}".encode()

    def store(self, task_id: str, cluster: str, model: str, vector: list[float], clean_seconds: float) -> None:
        nonce = os.urandom(12)
        sealed = self.key.encrypt(nonce, json.dumps(normalized(vector)).encode(), self._aad(task_id, cluster, model))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO prints VALUES (?,?,?,?,?,?,?)",
                            (task_id, cluster, model, nonce, sealed, clean_seconds, self.clock()))

    def load(self, task_id: str, model: str) -> dict[str, dict]:
        self.expire()
        rows = self.db.execute("SELECT * FROM prints WHERE task_id=? AND model=?", (task_id, model)).fetchall()
        return {row["cluster"]: {"vector": json.loads(self.key.decrypt(row["nonce"], row["ciphertext"],
                                                                       self._aad(task_id, row["cluster"], model))),
                                 "clean_seconds": row["clean_seconds"]} for row in rows}

    def store_windows(self, task_id, cluster, model, vectors, clean_seconds, policy, evidence):
        """Legacy matching needs independent votes; a centroid cannot replace them.

        A separate encrypted table preserves the existing centroid schema/AAD and
        allows both crop policies for one cluster without overwriting either.
        """
        value = {'vectors': [normalized(v) for v in vectors], 'clean_seconds': clean_seconds,
                 'policy': policy, 'evidence': evidence}
        nonce = os.urandom(12)
        sealed = self.key.encrypt(nonce, json.dumps(value).encode(), self._aad(task_id, cluster, model)+b':windows')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO window_prints VALUES (?,?,?,?,?,?)',
                            (task_id, cluster, model, nonce, sealed, self.clock()))

    def load_windows(self, task_id, model, policy):
        self.expire()
        rows = self.db.execute('SELECT * FROM window_prints WHERE task_id=? AND model=?', (task_id, model)).fetchall()
        result = {}
        for row in rows:
            value = json.loads(self.key.decrypt(row['nonce'], row['ciphertext'],
                                               self._aad(task_id, row['cluster'], model)+b':windows'))
            if value['policy'] == policy:
                result[row['cluster']] = value
        return result

    def forget(self, task_id: str, cluster: str | None = None) -> int:
        """Erase a meeting's prints, or one speaker's once the owner confirmed or dismissed it."""
        with self.db:
            if cluster is None:
                removed = self.db.execute("DELETE FROM prints WHERE task_id=?", (task_id,)).rowcount
            else:
                removed = self.db.execute("DELETE FROM prints WHERE task_id=? AND cluster=?", (task_id, cluster)).rowcount
            if cluster is None:
                removed += self.db.execute('DELETE FROM window_prints WHERE task_id=?', (task_id,)).rowcount
            else:
                removed += self.db.execute('DELETE FROM window_prints WHERE task_id=? AND cluster=?', (task_id, cluster)).rowcount
        self.db.execute("VACUUM")
        return removed

    def expire(self) -> int:
        with self.db:
            removed = self.db.execute("DELETE FROM prints WHERE created_at < ?",
                                      (self.clock() - RETENTION_SECONDS,)).rowcount
            removed += self.db.execute('DELETE FROM window_prints WHERE created_at < ?',
                                       (self.clock() - RETENTION_SECONDS,)).rowcount
        if removed:
            self.db.execute("VACUUM")
        return removed


def open_prints() -> MeetingPrints:
    from .keychain import obtain
    from .onboarding import ROOT
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    return MeetingPrints(ROOT / "meeting-prints.sqlite3", obtain(ROOT / "vault-key"))
