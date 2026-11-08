"""Durable, per-source acoustic blocks from the native capture watermark.

Capture keeps writing while inference is behind. Only fsynced prefixes are read;
restart replays preprocessing from the beginning and reuses completed ASR blocks.
No model, identity, audio device or network service is owned by this module.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
from uuid import UUID

RATE = 48000
BLOCK_FRAMES = 12 * RATE
RESAMPLING_POLICY = "fir127_hamming_7200hz_decimate3_v1"


class SourceReader:
    def __init__(self, folder):
        self.folder = Path(folder).resolve(strict=True)
        self.directory = self.folder / "sources"
        if self.directory.is_symlink() or self.directory.resolve() != self.directory:
            raise ValueError("incremental_source_path_invalid")
        self.capture_id, self.previous = None, {}

    def progress(self):
        path = self.directory / "progress.json"
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError("incremental_progress_invalid")
        value = json.loads(path.read_text())
        capture_id = str(UUID(value["capture_id"]))
        frames = value["sources"]
        if (value.get("version") != 1 or value.get("state") not in ("open", "closed")
                or value.get("clock") != "recording_elapsed" or value.get("sample_rate") != RATE
                or value.get("sample_format") != "pcm16" or not isinstance(frames, dict) or not frames
                or set(frames) - {"microphone", "system"}
                or any(type(n) is not int or not 0 <= n <= RATE*86400 for n in frames.values())
                or self.capture_id is not None and capture_id != self.capture_id
                or self.previous and set(frames) != set(self.previous)
                or any(n < self.previous.get(key, 0) for key, n in frames.items())):
            raise ValueError("incremental_progress_invalid")
        self.capture_id, self.previous = capture_id, frames
        return value

    def samples(self, source, start, end, progress):
        """FIR guards are read from the same immutable prefix, never per-block resets."""
        import numpy as np
        available = progress["sources"][source]
        half = 63
        if start % 3 or not 0 <= start < end:
            raise ValueError("incremental_range_invalid")
        if progress["state"] != "closed" and end + half > available:
            raise ValueError("incremental_prefix_not_ready")
        left = min(available, max(0, start-half))
        right = max(left, min(end+half, available))
        # After a track is renamed but before the closed watermark, its durable
        # prefix is still valid. Header byte counts are not the commit marker.
        path = self.directory / (source + ".wav.part")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            fd = os.open(path.with_suffix(""), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            header = os.pread(fd, 44, 0)
            if (len(header) != 44 or header[:4] != b"RIFF" or header[8:16] != b"WAVEfmt "
                    or struct.unpack_from("<HHI", header, 20) != (1, 1, RATE)
                    or header[36:40] != b"data" or struct.unpack_from("<H", header, 34)[0] != 16
                    or os.fstat(fd).st_size < 44 + available*2):
                raise ValueError("incremental_source_invalid")
            raw = os.pread(fd, max(0, right-left)*2, 44+left*2)
            if len(raw) != max(0, right-left)*2:
                raise ValueError("incremental_source_truncated")
        finally:
            os.close(fd)
        pcm = np.frombuffer(raw, "<i2").astype("float32") / 32768
        # A closed shorter source contributes explicit tail silence. While open
        # both tracks must have committed the prefix; never guess missing audio.
        pad_left = max(0, left-(start-half)) if raw else 0
        pad_right = end-start+2*half-len(pcm)-pad_left
        pcm = np.pad(pcm, (pad_left, pad_right))
        if len(pcm) != end-start+2*half:
            raise ValueError("incremental_resampling_input_invalid")
        x = np.arange(-half, half+1)
        h = 2*(7200/RATE)*np.sinc(2*(7200/RATE)*x)*np.hamming(2*half+1)
        h /= h.sum()
        signal = np.convolve(pcm, h, mode="valid")[::3].astype("float32")
        digest = hashlib.sha256(raw + json.dumps([source, start, end, pad_left, pad_right, RESAMPLING_POLICY]).encode()).hexdigest()
        return signal, digest


class BlockJournal:
    def __init__(self, path, capture_id, policy):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("incremental_journal_path_invalid")
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""CREATE TABLE IF NOT EXISTS capture (
            id TEXT PRIMARY KEY, policy TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS blocks (
            sequence INTEGER PRIMARY KEY, source TEXT NOT NULL, start_frame INTEGER NOT NULL,
            end_frame INTEGER NOT NULL, input_hash TEXT NOT NULL, state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, result TEXT, UNIQUE(source,start_frame,end_frame));""")
        row = self.db.execute("SELECT * FROM capture").fetchone()
        if row and (row["id"] != capture_id or row["policy"] != policy):
            self.db.close()
            raise ValueError("incremental_journal_policy_mismatch")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO capture VALUES (?,?)", (capture_id, policy))

    def cached(self, source, start, end, digest):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO blocks(source,start_frame,end_frame,input_hash,state) VALUES (?,?,?,?,'pending')",
                            (source, start, end, digest))
            row = self.db.execute("SELECT * FROM blocks WHERE source=? AND start_frame=? AND end_frame=?", (source, start, end)).fetchone()
            if row["input_hash"] != digest:
                raise ValueError("incremental_block_source_changed")
            return json.loads(row["result"]) if row["state"] == "complete" else None

    def attempt(self, source, start, end):
        with self.db:
            self.db.execute("UPDATE blocks SET attempts=attempts+1 WHERE source=? AND start_frame=? AND end_frame=?",
                            (source, start, end))

    def complete(self, source, start, end, result):
        with self.db:
            self.db.execute("UPDATE blocks SET state='complete',result=? WHERE source=? AND start_frame=? AND end_frame=?",
                            (json.dumps(result, ensure_ascii=False), source, start, end))

    def events(self, after=0, limit=64, through_frame=None):
        if (type(after) is not int or not 0 <= after < 2**63 or type(limit) is not int or not 1 <= limit <= 256):
            raise ValueError("incremental_cursor_invalid")
        return [{"sequence": r["sequence"], "source": r["source"], "start_frame": r["start_frame"],
                 "end_frame": r["end_frame"], "result": json.loads(r["result"])} for r in
                self.db.execute("""SELECT * FROM blocks WHERE sequence>? AND state='complete'
                    AND (? IS NULL OR end_frame<=?) ORDER BY sequence LIMIT ?""", (after, through_frame, through_frame, limit))]

    def close(self):
        self.db.close()


class IncrementalProcessor:
    def __init__(self, reader, journal, recognize, prepare_pair=None):
        self.reader, self.journal, self.recognize = reader, journal, recognize
        self.prepare_pair = prepare_pair
        self.position, self.chain, self.failed = 0, "", False

    def poll(self):
        if self.failed:
            raise ValueError("incremental_restart_required")
        progress = self.reader.progress()
        available = (max if progress["state"] == "closed" else min)(progress["sources"].values())
        end = min(self.position + BLOCK_FRAMES, available)
        if end <= self.position or progress["state"] == "open" and end+63 > available:
            return {"state": "drained" if progress["state"] == "closed" else "waiting", "events": []}
        # Never finalize a short block simply because one callback is delayed.
        if progress["state"] == "open" and end-self.position < BLOCK_FRAMES:
            return {"state": "waiting", "events": []}
        try:
            pairs = {source: self.reader.samples(source, self.position, end, progress)
                     for source in sorted(progress["sources"])}
            chain = hashlib.sha256(json.dumps([self.chain, {s: p[1] for s, p in pairs.items()}], sort_keys=True).encode()).hexdigest()
            signals = {s: p[0] for s, p in pairs.items()}
            if self.prepare_pair:
                signals = self.prepare_pair(signals)
            import numpy as np
            if (set(signals) != set(pairs) or any(not isinstance(v, np.ndarray) or v.ndim != 1
                    or len(v) != (end-self.position+2)//3 or not np.isfinite(v).all() for v in signals.values())):
                raise ValueError("incremental_preprocessing_invalid")
            reused, completed = 0, []
            for source in sorted(signals):
                result = self.journal.cached(source, self.position, end, chain)
                if result is None:
                    self.journal.attempt(source, self.position, end)
                    result = self.recognize(source, signals[source], self.position/RATE, end/RATE)
                    if (not isinstance(result, dict) or not isinstance(result.get("text"), str)
                            or len(result["text"]) > 20000):
                        raise ValueError("incremental_asr_output_invalid")
                    self.journal.complete(source, self.position, end, result)
                else:
                    reused += 1
                completed.append(source)
            self.position, self.chain = end, chain
            return {"state": "processed", "completed_sources": completed, "reused_sources": reused,
                    "end_frame": end}
        except BaseException:
            # Stateful AEC already consumed this block. A fresh processor must
            # replay preprocessing before retrying; completed ASR remains cached.
            self.failed = True
            raise
