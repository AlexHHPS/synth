#!/usr/bin/env python3
"""Bounded cleanup of Synth-owned audio; no user-selected external originals."""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from synth.worker.journal import Journal, TTL_SECONDS


def sweep_native(raw, now=None, embedding_temporaries=False):
    raw = Path(raw)
    if not raw.exists():
        return 0
    if raw.is_symlink():
        raise ValueError("native_raw_root_symlink")
    root = raw.resolve()
    now = time.time() if now is None else now
    removed = 0
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if not path.resolve().is_relative_to(root):
            continue
        is_embedding_temporary = embedding_temporaries and path.name == "result.json" and path.parent.name.startswith("speaker-")
        if not is_embedding_temporary and path.suffix.lower() not in {".wav", ".mp4", ".m4a", ".flac", ".mp3", ".ogg", ".opus", ".webm", ".aac", ".aiff", ".aif", ".part"}:
            continue
        stat = path.stat()
        age_start = min(stat.st_mtime, getattr(stat, "st_birthtime", stat.st_mtime))
        if now - age_start >= TTL_SECONDS:
            path.unlink()
            removed += 1
    return removed


def main():
    runtime = Path.home() / "Library/Application Support/dev.synth.voice" if getattr(sys, "frozen", False) else ROOT / "synth/.runtime"
    journal = Journal(runtime / "capture-journal")
    try:
        journal.cleanup()
    finally:
        journal.close()
    from synth.worker.desktop_pipeline import STATE, CAPTURE, delete_processed_audio
    from synth.worker.desktop_queue import DesktopQueue
    completed_count = 0
    if (STATE / "queue.sqlite3").exists():
        queue = DesktopQueue(STATE / "queue.sqlite3", CAPTURE)
        try:
            rows = queue.db.execute("SELECT * FROM desktop_tasks WHERE state='succeeded'").fetchall()
            for row in rows:
                task = queue.decode(row)
                if (STATE / "tasks" / task["id"] / "identity-status.json").exists():
                    completed_count += delete_processed_audio(task, CAPTURE)
        finally:
            queue.close()
    count = sweep_native(Path.home() / "Library/Application Support/dev.synth.voice/capture/raw")
    voice_count = sweep_native(Path.home() / "Library/Application Support/dev.synth.voice/voice/onboarding/raw", embedding_temporaries=True)
    state = runtime / "retention-status.json"
    state.write_text(json.dumps({"last_run_unix": time.time(), "native_expired_files_removed": count, "completed_capture_files_removed": completed_count, "voice_expired_files_removed": voice_count,
                                "ttl_seconds": TTL_SECONDS, "status": "ok"}) + "\n")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError):
        print("Retention cleanup failed; inspect the Synth runtime directory.", file=sys.stderr)
        sys.exit(1)
