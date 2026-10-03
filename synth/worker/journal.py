"""Mac-owned capture journal. Raw copies are temporary; originals stay untouched."""
import hashlib
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4
import wave

TTL_SECONDS = 24 * 60 * 60


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Journal:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.raw = self.root / "raw"
        self.raw.mkdir(exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / "journal.sqlite")
        os.chmod(self.root / "journal.sqlite", 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS meetings (
            id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at REAL NOT NULL,
            asr_done INTEGER NOT NULL DEFAULT 0, identity_done INTEGER NOT NULL DEFAULT 0
          );
          CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, kind TEXT NOT NULL,
            expected_chunks INTEGER, FOREIGN KEY(meeting_id) REFERENCES meetings(id)
          );
          CREATE TABLE IF NOT EXISTS chunks (
            source_id TEXT NOT NULL, sequence INTEGER NOT NULL, start_ms INTEGER NOT NULL,
            duration_ms INTEGER NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL,
            created_at REAL NOT NULL, deleted_at REAL,
            PRIMARY KEY(source_id,sequence)
          );
          CREATE TABLE IF NOT EXISTS temporary_files (
            path TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, created_at REAL NOT NULL
          );
          CREATE TABLE IF NOT EXISTS events (
            sequence INTEGER PRIMARY KEY, meeting_id TEXT NOT NULL,
            action TEXT NOT NULL, created_at REAL NOT NULL
          );
        """)
        self.db.execute("PRAGMA foreign_keys=ON")

    def close(self):
        self.db.close()

    def event(self, meeting, action):
        self.db.execute("INSERT INTO events(meeting_id,action,created_at) VALUES (?,?,?)", (meeting, action, time.time()))

    def start(self, kinds):
        if not kinds or len(set(kinds)) != len(kinds) or any(k not in ("microphone", "system", "import") for k in kinds):
            raise ValueError("invalid_source_kinds")
        meeting, sources = str(uuid4()), {}
        with self.db:
            self.db.execute("INSERT INTO meetings(id,state,created_at) VALUES (?,'capturing',?)", (meeting, time.time()))
            for kind in kinds:
                source = str(uuid4())
                self.db.execute("INSERT INTO sources(id,meeting_id,kind) VALUES (?,?,?)", (source, meeting, kind))
                sources[kind] = source
            self.event(meeting, "capture.start")
        return meeting, sources

    def source(self, source_id):
        row = self.db.execute("SELECT s.*,m.state FROM sources s JOIN meetings m ON m.id=s.meeting_id WHERE s.id=?", (source_id,)).fetchone()
        if not row:
            raise ValueError("source_missing")
        return row

    def append(self, source_id, sequence, start_ms, wav):
        source = self.source(source_id)
        if sequence < 0 or start_ms < 0:
            raise ValueError("invalid_chunk_position")
        wav = Path(wav)
        hashed = file_hash(wav)
        with wave.open(str(wav)) as audio:
            if audio.getcomptype() != "NONE" or audio.getsampwidth() != 2 or audio.getnframes() < 1:
                raise ValueError("unsupported_capture_wav")
            duration = round(audio.getnframes() * 1000 / audio.getframerate())
        old = self.db.execute("SELECT sha256,start_ms FROM chunks WHERE source_id=? AND sequence=?", (source_id, sequence)).fetchone()
        if old:
            if old["sha256"] != hashed or old["start_ms"] != start_ms:
                raise ValueError("chunk_idempotency_conflict")
            return False
        if source["state"] != "capturing" or (source["expected_chunks"] is not None and sequence >= source["expected_chunks"]):
            raise ValueError("capture_closed")
        path = self.raw / source["meeting_id"] / source_id / f"{sequence:08d}.wav"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".part")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with wav.open("rb") as src, os.fdopen(fd, "wb") as dst:
            for block in iter(lambda: src.read(1024*1024), b""):
                dst.write(block)
            dst.flush()
            os.fsync(dst.fileno())
        if file_hash(temporary) != hashed:
            temporary.unlink()
            raise ValueError("capture_copy_changed")
        temporary.replace(path)
        with self.db:
            self.db.execute("INSERT INTO chunks(source_id,sequence,start_ms,duration_ms,sha256,path,created_at) VALUES (?,?,?,?,?,?,?)",
                            (source_id, sequence, start_ms, duration, hashed, str(path), time.time()))
            self.event(source["meeting_id"], "capture.chunk")
        return True

    def seal_source(self, source_id, expected_chunks):
        source = self.source(source_id)
        if expected_chunks < 1 or source["state"] != "capturing":
            raise ValueError("invalid_source_eof")
        if source["expected_chunks"] is not None and source["expected_chunks"] != expected_chunks:
            raise ValueError("source_eof_conflict")
        with self.db:
            self.db.execute("UPDATE sources SET expected_chunks=? WHERE id=?", (expected_chunks, source_id))
            self.event(source["meeting_id"], "capture.source_eof")

    def seal(self, meeting):
        sources = self.db.execute("SELECT id,expected_chunks FROM sources WHERE meeting_id=?", (meeting,)).fetchall()
        if not sources:
            raise ValueError("meeting_missing")
        for source in sources:
            chunks = self.db.execute("SELECT * FROM chunks WHERE source_id=? ORDER BY sequence", (source["id"],)).fetchall()
            if source["expected_chunks"] is None or [c["sequence"] for c in chunks] != list(range(source["expected_chunks"])):
                raise ValueError("capture_missing_chunk")
            end = 0
            for chunk in chunks:
                if chunk["deleted_at"] or not Path(chunk["path"]).exists() or file_hash(chunk["path"]) != chunk["sha256"]:
                    raise ValueError("capture_chunk_unavailable")
                if chunk["start_ms"] < end:
                    raise ValueError("capture_chunk_overlap")
                end = chunk["start_ms"] + chunk["duration_ms"]
        with self.db:
            self.db.execute("UPDATE meetings SET state='sealed' WHERE id=? AND state='capturing'", (meeting,))
            self.event(meeting, "capture.sealed")

    def track_temporary(self, meeting, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("temporary_path_outside_journal")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO temporary_files VALUES (?,?,?)", (str(path), meeting, time.time()))

    def mark_processed(self, meeting, stage):
        column = {"asr": "asr_done", "identity": "identity_done"}.get(stage)
        if not column:
            raise ValueError("invalid_processing_stage")
        with self.db:
            changed = self.db.execute(f"UPDATE meetings SET {column}=1 WHERE id=? AND state IN ('sealed','processed')", (meeting,)).rowcount
            if not changed:
                raise ValueError("meeting_not_sealed")
            self.event(meeting, "processing." + stage)
        self.cleanup()

    def remove_owned(self, path):
        path = Path(path)
        if not path.resolve().is_relative_to(self.root) or path.is_symlink():
            raise ValueError("cleanup_path_not_owned")
        path.unlink(missing_ok=True)

    def cleanup(self, now=None):
        now = time.time() if now is None else now
        done = {r["id"] for r in self.db.execute("SELECT id FROM meetings WHERE asr_done=1 AND identity_done=1")}
        rows = self.db.execute("SELECT c.*,s.meeting_id FROM chunks c JOIN sources s ON s.id=c.source_id WHERE c.deleted_at IS NULL").fetchall()
        with self.db:
            for row in rows:
                expired = now-row["created_at"] >= TTL_SECONDS
                if row["meeting_id"] in done or expired:
                    self.remove_owned(row["path"])
                    self.db.execute("UPDATE chunks SET deleted_at=? WHERE source_id=? AND sequence=?", (now, row["source_id"], row["sequence"]))
                    if expired and row["meeting_id"] not in done:
                        self.db.execute("UPDATE meetings SET state='expired' WHERE id=?", (row["meeting_id"],))
                    self.event(row["meeting_id"], "audio.delete")
            for row in self.db.execute("SELECT * FROM temporary_files").fetchall():
                if row["meeting_id"] in done or now-row["created_at"] >= TTL_SECONDS:
                    self.remove_owned(row["path"])
                    self.db.execute("DELETE FROM temporary_files WHERE path=?", (row["path"],))
            for meeting in done:
                self.db.execute("UPDATE meetings SET state='processed' WHERE id=?", (meeting,))
        # Recover filesystem writes interrupted before their SQLite insert.
        for path in self.raw.rglob("*"):
            if path.is_file() and now-path.stat().st_mtime >= TTL_SECONDS:
                self.remove_owned(path)
