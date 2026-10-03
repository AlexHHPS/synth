"""A filesystem retention walk on temporary fixtures, outside live capture data."""
import importlib.util
import os
from pathlib import Path
import tempfile
import time

source = Path(__file__).resolve().parents[1] / "scripts/retention.py"
spec = importlib.util.spec_from_file_location("retention", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    raw = root / "raw"
    raw.mkdir()
    expired = raw / "audio.mp4"
    current = raw / "current.wav"
    document = raw / "transcripts.json"
    original = root / "original.wav"
    for path in (expired, current, document, original):
        path.write_bytes(b"fixture")
    now = time.time()
    imported_formats = [raw / ("expired" + suffix) for suffix in (".ogg", ".opus", ".webm", ".aac", ".aiff", ".aif")]
    for path in imported_formats:
        path.write_bytes(b"fixture")
        os.utime(path, (now - 90000, now - 90000))
    os.utime(expired, (now - 90000, now - 90000))
    os.utime(document, (now - 90000, now - 90000))
    os.utime(original, (now - 90000, now - 90000))
    (raw / "link.wav").symlink_to(original)
    assert module.sweep_native(raw, now) == 1 + len(imported_formats)
    assert not expired.exists()
    assert all(not path.exists() for path in imported_formats)
    assert current.exists() and document.exists() and original.exists()
    assert (raw / "link.wav").is_symlink()
print("RETENTION_WALK_OK; expired owned audio removed; original and text preserved")
