"""Durable host-side acoustic queue; stale owners cannot commit queue results.

This owns job state only. Audio capture, inference, API calls and retention stay
in their separate adapters. One active lease serializes scheduling. The host
coordinator must also hold a process lock across inference, keep a heartbeat,
and use API idempotency for external writes: a queue fence alone cannot undo
an HTTP request or stop an expired worker's running native subprocess.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import UUID, uuid4


class QueueError(ValueError):
    pass


class DesktopQueue:
    def __init__(self, path, capture_root):
        self.capture_root = Path(capture_root).resolve(strict=True)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS desktop_tasks (
                id TEXT PRIMARY KEY,
                capture_key TEXT NOT NULL UNIQUE,
                metadata TEXT NOT NULL,
                state TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT 'queued',
                checkpoints TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                lease_until REAL,
                lease_token TEXT,
                epoch INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                error_code TEXT,
                result TEXT
            );
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def enqueue(self, sources, title, folder_id=None, consent_confirmed=False, capture_id=None, notes="", owner_id=None):
        if owner_id is not None:
            owner_id = str(UUID(str(owner_id)))
        if consent_confirmed is not True:
            raise QueueError("capture_consent_not_confirmed")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 300:
            raise QueueError("capture_title_invalid")
        if not isinstance(notes, str) or len(notes) > 200000:
            raise QueueError("capture_notes_invalid")
        if folder_id is not None:
            try:
                folder_id = str(UUID(folder_id))
            except (ValueError, TypeError, AttributeError):
                raise QueueError("capture_folder_invalid") from None
        if not isinstance(sources, dict) or not sources or set(sources) - {"microphone", "system", "import"}:
            raise QueueError("capture_sources_invalid")
        if "import" in sources and len(sources) != 1:
            raise QueueError("capture_sources_invalid")
        if any(not isinstance(path, (str, Path)) for path in sources.values()):
            raise QueueError("capture_file_invalid")
        if capture_id is not None:
            try:
                capture_id = str(UUID(capture_id))
            except (ValueError, TypeError, AttributeError):
                raise QueueError("capture_receipt_invalid") from None
            receipt_key = "receipt:" + capture_id
            with self.transaction():
                old = self.db.execute("SELECT * FROM desktop_tasks WHERE capture_key=?", (receipt_key,)).fetchone()
                if old:
                    metadata = json.loads(old["metadata"])
                    requested = {kind: str(Path(path).resolve()) for kind, path in sources.items()}
                    stored = {kind: value["path"] for kind, value in metadata["sources"].items()}
                    if requested != stored or title.strip() != metadata["title"] or folder_id != metadata["folder_id"] or notes != metadata.get("notes", "") or owner_id != metadata.get("owner_id"):
                        raise QueueError("capture_idempotency_conflict")
                    return self.decode(old)
        descriptors = {}
        files_seen = set()
        for kind, filename in sorted(sources.items()):
            if not isinstance(filename, (str, Path)):
                raise QueueError("capture_file_invalid")
            supplied = Path(filename)
            try:
                path = supplied.resolve(strict=True)
            except (OSError, RuntimeError):
                raise QueueError("capture_file_unavailable") from None
            if not path.is_relative_to(self.capture_root) or not path.is_file():
                raise QueueError("capture_path_not_owned")
            if supplied.is_symlink() or path.stat().st_size == 0:
                raise QueueError("capture_file_invalid")
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity in files_seen:
                raise QueueError("capture_sources_share_file")
            files_seen.add(identity)
            if path.suffix.lower() not in {".wav", ".mp4", ".m4a", ".flac", ".mp3", ".ogg", ".opus", ".webm"}:
                raise QueueError("capture_file_invalid")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            descriptors[kind] = {"path": str(path), "sha256": digest, "bytes": path.stat().st_size}
        metadata = {"sources": descriptors, "title": title.strip(), "folder_id": folder_id, "consent_confirmed": True}
        if owner_id is not None:
            metadata["owner_id"] = owner_id
        if capture_id is not None:
            metadata["capture_id"] = capture_id
        if notes:
            metadata["notes"] = notes
        # One captured set produces one task. Different requested options conflict,
        # rather than silently creating a second meeting after a retransmission.
        key = "receipt:" + capture_id if capture_id else hashlib.sha256(self.encode(descriptors).encode()).hexdigest()
        now = time.time()
        with self.transaction():
            old = self.db.execute("SELECT * FROM desktop_tasks WHERE capture_key=?", (key,)).fetchone()
            if old:
                if json.loads(old["metadata"]) != metadata:
                    raise QueueError("capture_idempotency_conflict")
                return self.decode(old)
            identifier = str(uuid4())
            self.db.execute("INSERT INTO desktop_tasks(id,capture_key,metadata,state,created_at,updated_at) VALUES (?,?,?,'queued',?,?)",
                            (identifier, key, self.encode(metadata), now, now))
            return self.get(identifier)

    @staticmethod
    def encode(value):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(text.encode()) > 1024 * 1024:
            raise QueueError("checkpoint_too_large")
        return text

    @staticmethod
    def decode(row):
        result = dict(row)
        for field in ("metadata", "checkpoints", "result"):
            result[field] = json.loads(result[field]) if result[field] else None
        return result

    def get(self, identifier):
        row = self.db.execute("SELECT * FROM desktop_tasks WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise QueueError("task_missing")
        return self.decode(row)

    def claim(self, lease_seconds=60, now=None):
        now = time.time() if now is None else now
        if not 1 <= lease_seconds <= 600:
            raise QueueError("lease_duration_invalid")
        with self.transaction():
            # Recover only expired owners; live inferences keep their lease through
            # heartbeat. Recovery keeps checkpoints but invalidates the old token.
            self.db.execute("UPDATE desktop_tasks SET state=CASE WHEN state='cancel_requested' THEN 'cancelled' ELSE 'queued' END,lease_token=NULL,lease_until=NULL,epoch=epoch+1,updated_at=? WHERE state IN ('running','cancel_requested') AND lease_until<=?", (now, now))
            if self.db.execute("SELECT 1 FROM desktop_tasks WHERE state IN ('running','cancel_requested')").fetchone():
                return None
            row = self.db.execute("SELECT id FROM desktop_tasks WHERE state='queued' ORDER BY created_at,rowid LIMIT 1").fetchone()
            if row is None:
                return None
            token = str(uuid4())
            self.db.execute("UPDATE desktop_tasks SET state='running',lease_token=?,lease_until=?,epoch=epoch+1,attempts=attempts+1,updated_at=?,error_code=NULL WHERE id=?", (token, now + lease_seconds, now, row["id"]))
            return self.get(row["id"])

    def owned(self, identifier, token, now):
        row = self.get(identifier)
        if row["state"] not in {"running", "cancel_requested"} or row["lease_token"] != token or row["lease_until"] <= now:
            raise QueueError("lease_stale")
        return row

    def heartbeat(self, identifier, token, lease_seconds=60, now=None):
        now = time.time() if now is None else now
        if not 1 <= lease_seconds <= 600:
            raise QueueError("lease_duration_invalid")
        with self.transaction():
            row = self.owned(identifier, token, now)
            self.db.execute("UPDATE desktop_tasks SET lease_until=?,updated_at=? WHERE id=?", (now + lease_seconds, now, identifier))
            return row["state"] != "cancel_requested"

    def checkpoint(self, identifier, token, stage, descriptor, now=None):
        if stage not in {"normalized", "asr", "diarization", "canonical", "identified", "uploaded"}:
            raise QueueError("checkpoint_stage_invalid")
        now = time.time() if now is None else now
        with self.transaction():
            row = self.owned(identifier, token, now)
            if row["state"] == "cancel_requested":
                raise QueueError("task_cancel_requested")
            checkpoints = row["checkpoints"]
            if stage in checkpoints and checkpoints[stage] != descriptor:
                raise QueueError("checkpoint_conflict")
            checkpoints[stage] = descriptor
            self.db.execute("UPDATE desktop_tasks SET checkpoints=?,stage=?,updated_at=? WHERE id=?", (self.encode(checkpoints), stage, now, identifier))

    def finish(self, identifier, token, result=None, error_code=None, now=None):
        now = time.time() if now is None else now
        if error_code is not None and (not isinstance(error_code, str) or not error_code.replace('_', '').isalnum() or len(error_code) > 100):
            raise QueueError("error_code_invalid")
        with self.transaction():
            row = self.owned(identifier, token, now)
            if row["state"] == "cancel_requested":
                state, result, error_code = "cancelled", None, None
            else:
                state = "failed" if error_code else "succeeded"
                if error_code:
                    result = None
                if state == "succeeded" and not isinstance(result, dict):
                    raise QueueError("task_result_missing")
            self.db.execute("UPDATE desktop_tasks SET state=?,result=?,error_code=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?", (state, self.encode(result) if result is not None else None, error_code, now, identifier))
            return self.get(identifier)

    def cancel(self, identifier):
        with self.transaction():
            row = self.get(identifier)
            state = {"queued": "cancelled", "running": "cancel_requested"}.get(row["state"], row["state"])
            self.db.execute("UPDATE desktop_tasks SET state=?,updated_at=? WHERE id=?", (state, time.time(), identifier))
            return self.get(identifier)

    def retry(self, identifier):
        with self.transaction():
            row = self.get(identifier)
            if row["state"] != "failed":
                raise QueueError("task_not_failed")
            self.db.execute("UPDATE desktop_tasks SET state='queued',error_code=NULL,updated_at=? WHERE id=?", (time.time(), identifier))
            return self.get(identifier)
