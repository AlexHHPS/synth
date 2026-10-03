"""Mechanical lifecycle tests use invented vectors, never claim acoustic accuracy."""
from pathlib import Path
import tempfile

from synth.speakers.profiles import Vault


def main():
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(dir=root / "synth/.runtime") as temporary:
        path = Path(temporary)
        (path / "key").write_text("12" * 32)
        vault = Vault(path / "profiles.db", path / "key")
        quality = {"clean_speech_seconds": 20, "clipped_fraction": 0, "rms_dbfs": -20, "overlap": False}
        a, b = [1.0] + [0.0]*255, [0.0, 1.0] + [0.0]*254
        calibration = {"state": "FROZEN", "model": "test-only", "threshold": .8, "margin": .1, "dataset_hash": "mechanical-test", "id": "test-only"}
        pid = vault.enroll("alice", "test-only", [a]*3, "mechanical-consent", ["testhash"], quality)
        row = vault.db.execute("SELECT ciphertext FROM profiles WHERE id=?", (pid,)).fetchone()
        assert b"[1.0, 0.0, 0.0" not in row["ciphertext"]
        assert vault.match(a, "test-only", {}, clean_seconds=5)["employee_id"] is None
        assert vault.match(a, "test-only", calibration, overlap=True, clean_seconds=5)["employee_id"] is None
        assert vault.match(a, "different-model", calibration, clean_seconds=5)["employee_id"] is None
        assert vault.match(a, "test-only", calibration, clean_seconds=5)["employee_id"] == "alice"
        assert vault.match(b, "test-only", calibration, clean_seconds=5)["employee_id"] is None
        vault.enroll("bob", "test-only", [a]*3, "mechanical-consent", ["testhash2"], quality)
        assert vault.match(a, "test-only", calibration, clean_seconds=5)["reason"] == "ambiguous"
        vault.revoke(pid)
        assert vault.match(a, "test-only", calibration, clean_seconds=5)["employee_id"] == "bob"
        vault.delete(pid)
        assert vault.db.execute("SELECT 1 FROM profiles WHERE id=?", (pid,)).fetchone() is None
        try:
            vault.enroll("alice", "test-only", [a]*3, "mechanical-consent", ["testhash"], {})
        except ValueError:
            pass
        else:
            raise AssertionError("low quality enrollment accepted")
        vault.close()
    print("PROFILE_VAULT_WALK_OK; invented vectors; no acoustic acceptance claim")


if __name__ == "__main__":
    main()
