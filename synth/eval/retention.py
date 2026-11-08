"""Opt-in evaluation audio retention: encrypted, host-only, purged on demand.

Meetings without the eval mark keep the default policy: audio is erased right
after acoustic processing. Marked meetings keep an AES-GCM copy of each
normalized source under the eval store until `python3 -m synth.eval purge`.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

APPDATA = Path.home() / "Library/Application Support/dev.synth.voice"
STORE = APPDATA / "eval"
KEY_ACCOUNT = "synth-voice-eval"


def keychain_key() -> bytes:
    from synth.speakers import keychain
    key = keychain.read(KEY_ACCOUNT)
    if key is None:
        key = secrets.token_bytes(32)
        keychain.write(KEY_ACCOUNT, key)
    return key


def is_eval(task: dict) -> bool:
    return task.get("metadata", {}).get("eval_retention") is True


def retain(task: dict, normalized: dict, store: Path = STORE,
           key: Callable[[], bytes] = keychain_key) -> dict:
    """Encrypt each normalized source of an eval-marked task; return the receipt."""
    if not is_eval(task):
        return {"retained": False}
    target = Path(store) / task["id"]
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    cipher = AESGCM(key())
    files = {}
    for source, descriptor in sorted(normalized.items()):
        nonce = os.urandom(12)
        aad = (task["id"] + ":" + source).encode()
        sealed = cipher.encrypt(nonce, Path(descriptor["path"]).read_bytes(), aad)
        path = target / (source + ".wav.enc")
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(nonce + sealed)
        files[source] = {"file": path.name, "sha256": descriptor["sha256"]}
    receipt = {"retained": True, "task_id": task["id"], "title": task["metadata"].get("title"),
               "files": files, "at": time.time()}
    (target / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    (target / "receipt.json").chmod(0o600)
    return receipt


def open_retained(task_id: str, source: str, store: Path = STORE,
                  key: Callable[[], bytes] = keychain_key) -> bytes:
    blob = (Path(store) / task_id / (source + ".wav.enc")).read_bytes()
    return AESGCM(key()).decrypt(blob[:12], blob[12:], (task_id + ":" + source).encode())


def purge(store: Path = STORE, forget_key: Callable[[], None] | None = None) -> int:
    """Erase every retained eval meeting; returns how many meetings were removed."""
    store = Path(store)
    if not store.exists():
        return 0
    if store.is_symlink():
        raise ValueError("eval_store_symlink")
    removed = sum(1 for p in store.iterdir() if p.is_dir())
    shutil.rmtree(store)
    if forget_key is not None:
        forget_key()
    return removed


def forget_keychain_key() -> None:
    import subprocess
    from synth.speakers.keychain import SERVICE
    subprocess.run(["/usr/bin/security", "delete-generic-password", "-s", SERVICE, "-a", KEY_ACCOUNT],
                   capture_output=True, timeout=15)
