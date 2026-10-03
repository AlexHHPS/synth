"""Real filesystem journal recovery, input integrity and temporary audio retention."""
from pathlib import Path
import tempfile
import wave

from synth.worker.journal import Journal, TTL_SECONDS


def main():
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(dir=root / "synth/.runtime") as temporary:
        path = Path(temporary)
        original = path / "original.wav"
        with wave.open(str(original), "wb") as out:
            out.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
            out.writeframes(b"\x00\x00"*16_000)
        journal = Journal(path / "journal")
        mid, sources = journal.start(["microphone", "system"])
        for source in sources.values():
            assert journal.append(source, 0, 0, original)
            assert not journal.append(source, 0, 0, original)
            journal.seal_source(source, 2)
        try:
            journal.seal(mid)
        except ValueError as error:
            assert str(error) == "capture_missing_chunk"
        else:
            raise AssertionError("missing chunk accepted")
        journal.close()
        journal = Journal(path / "journal")
        # EOF fixes the count but permits retransmission of an absent earlier chunk.
        assert journal.db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2
        for source in sources.values():
            assert journal.append(source, 1, 1000, original)
        journal.seal(mid)
        journal.cleanup(now=10**12)
        assert original.exists()
        assert journal.db.execute("SELECT count(*) FROM chunks WHERE deleted_at IS NULL").fetchone()[0] == 0
        assert journal.db.execute("SELECT state FROM meetings WHERE id=?", (mid,)).fetchone()[0] == "expired"
        second, sources = journal.start(["import"])
        source = sources["import"]
        journal.append(source, 0, 0, original)
        journal.seal_source(source, 1)
        journal.seal(second)
        raw_path = Path(journal.db.execute("SELECT path FROM chunks WHERE source_id=?", (source,)).fetchone()[0])
        temporary_audio = path / "journal" / "mixed.wav"
        temporary_audio.write_bytes(original.read_bytes())
        journal.track_temporary(second, temporary_audio)
        journal.mark_processed(second, "asr")
        assert raw_path.exists() and temporary_audio.exists()
        journal.mark_processed(second, "identity")
        assert not raw_path.exists() and not temporary_audio.exists() and original.exists()
        journal.close()
    print("JOURNAL_WALK_OK: duplicate, missing chunk, recovery, hard TTL, both-stage deletion, temporary cleanup, originals preserved")


if __name__ == "__main__":
    main()
