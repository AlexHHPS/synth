"""Voice encryption keys in macOS Keychain; migration verifies before deleting a file."""
import secrets
import fcntl
import os
import shlex
import subprocess

SERVICE = "dev.synth.voice.voice-vault"


def read(account):
    value = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", SERVICE,
                            "-a", account, "-w"], capture_output=True, text=True, timeout=15)
    if value.returncode == 44:
        return None
    if value.returncode:
        raise ValueError("voice_keychain_unavailable")
    key = value.stdout.strip()
    if len(key) != 64:
        raise ValueError("voice_key_invalid")
    try:
        return bytes.fromhex(key)
    except ValueError:
        raise ValueError("voice_key_invalid") from None


def write(account, key):
    command = "add-generic-password -U -s " + shlex.quote(SERVICE) + " -a " + shlex.quote(account) + " -w " + shlex.quote(key.hex()) + "\n"
    result = subprocess.run(["/usr/bin/security", "-i"], input=command, text=True,
                            capture_output=True, timeout=15)
    if result.returncode or read(account) != key:
        raise ValueError("voice_keychain_write_failed")


def obtain(legacy, account="synth-voice"):
    fd = os.open(legacy.parent / "key-migration.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _obtain(legacy, account)
    finally:
        os.close(fd)


def _obtain(legacy, account):
    if legacy.is_symlink():
        raise ValueError("voice_key_invalid")
    key = read(account)
    old = bytes.fromhex(legacy.read_text().strip()) if legacy.exists() else None
    if old is not None and len(old) != 32:
        raise ValueError("voice_key_invalid")
    if key is not None and old is not None and key != old:
        raise ValueError("voice_key_migration_conflict")
    if key is None:
        key = old or secrets.token_bytes(32)
        write(account, key)
    if legacy.exists():
        legacy.unlink()
    return key
