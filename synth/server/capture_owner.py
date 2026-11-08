"""Bind new live source archives to their original authenticated Mac session."""
import json
from pathlib import Path
from uuid import UUID

from synth.worker.desktop_queue import QueueError


def validate_sources(sources, capture_id, identity, capture_root):
    if not isinstance(sources, dict):
        raise QueueError("capture_sources_invalid")
    root = Path(capture_root).resolve(strict=True)
    folders, live = set(), []
    for filename in sources.values():
        if not isinstance(filename, (str, Path)):
            raise QueueError("capture_file_invalid")
        path = Path(filename).resolve(strict=True)
        if not path.is_relative_to(root):
            raise QueueError("capture_path_not_owned")
        folder = path.parent.parent if path.parent.name == "sources" else path.parent
        folders.add(folder)
        if (folder/"sources/progress.json").exists():
            live.append(folder)
    if not live:
        return  # Legacy archives predate the incremental source protocol.
    if len(folders) != 1:
        raise QueueError("capture_sources_different_sessions")
    try:
        expected = str(UUID(capture_id))
    except (ValueError, TypeError, AttributeError):
        raise QueueError("capture_id_invalid") from None
    owner = live[0]/"capture-owner.json"
    if owner.is_symlink() or not owner.is_file() or owner.stat().st_size > 4096:
        raise QueueError("capture_owner_missing")
    try:
        binding = json.loads(owner.read_text())
    except (OSError, ValueError):
        raise QueueError("capture_owner_invalid") from None
    if (not isinstance(binding, dict) or binding.get("version") != 1
            or binding.get("capture_id") != expected or binding.get("identity") != identity):
        raise QueueError("capture_owner_mismatch")
