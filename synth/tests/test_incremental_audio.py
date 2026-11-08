"""Source clocks, durable watermarks and restart behavior; synthetic PCM only."""
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
import wave

import numpy as np

from synth.worker.incremental_audio import BlockJournal, IncrementalProcessor, SourceReader, RATE


class IncrementalAudioTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "sources").mkdir()
        self.capture = str(uuid4())
        self.frames = {}
        self.calls = []
        self.journal = None

    def track(self, source, seconds, signal=None):
        frames = round(seconds*RATE)
        pcm = np.full(frames, 8192 if source == "microphone" else -16384, dtype="<i2") if signal is None else (signal*32767).astype("<i2")
        with wave.open(str(self.root / "sources" / (source+".wav.part")), "wb") as audio:
            audio.setparams((1, 2, RATE, 0, "NONE", "not compressed")); audio.writeframes(pcm.tobytes())
        self.frames[source] = frames

    def progress(self, state="closed", frames=None):
        (self.root / "sources/progress.json").write_text(json.dumps({"version": 1, "capture_id": self.capture,
            "state": state, "clock": "recording_elapsed", "sample_rate": RATE, "sample_format": "pcm16",
            "sources": frames if frames is not None else self.frames}))

    def recognize(self, source, signal, start, end):
        self.calls.append((source, start, end, len(signal), float(np.mean(signal[100:-100]))))
        return {"text": f"Synthetic {source} {start}..{end}"}

    def processor(self, recognize=None, prepare=None):
        if self.journal is not None:
            self.journal.close()
        self.journal = BlockJournal(self.root/"state/blocks.sqlite3", self.capture, "synthetic-model+raw-context-v1")
        self.addCleanup(self.journal.close)
        return IncrementalProcessor(SourceReader(self.root), self.journal, recognize or self.recognize, prepare)

    def drain(self, processor):
        while (result := processor.poll())["state"] == "processed":
            pass
        return result

    def test_open_watermark_ignores_uncommitted_wav_bytes(self):
        self.track("microphone", 16)
        self.progress("open", {"microphone": 8*RATE})
        processor = self.processor()
        self.assertEqual(processor.poll()["state"], "waiting")
        self.assertEqual(self.calls, [])
        self.progress("open", {"microphone": 13*RATE})
        self.assertEqual(processor.poll()["state"], "processed")
        self.assertEqual(self.calls[0][1:4], (0, 12, 12*16000))
        self.assertEqual(processor.poll()["state"], "waiting")

    def test_channels_stay_separate_with_exact_tail_and_shared_clock(self):
        self.track("microphone", 17)
        self.track("system", 17)
        self.progress()
        self.assertEqual(self.drain(self.processor())["state"], "drained")
        self.assertEqual([(s, a, b, n) for s, a, b, n, _ in self.calls],
                         [("microphone", 0, 12, 192000), ("system", 0, 12, 192000),
                          ("microphone", 12, 17, 80000), ("system", 12, 17, 80000)])
        self.assertAlmostEqual(self.calls[0][4], .25, places=5)
        self.assertAlmostEqual(self.calls[1][4], -.5, places=5)

    def test_restart_replays_preprocessing_but_never_repeats_completed_asr(self):
        self.track("microphone", 25)
        self.track("system", 25)
        self.progress()
        self.drain(self.processor())
        original = self.journal.events()
        calls = []
        def prepare(signals):
            calls.append(len(signals["microphone"]))
            return signals
        def forbidden(*args):
            self.fail("Completed audio was sent to ASR again")
        self.drain(self.processor(forbidden, prepare))
        self.assertEqual(self.journal.events(), original)
        self.assertEqual(calls, [192000, 192000, 16000])
        self.assertEqual(self.journal.events(after=original[2]["sequence"]), original[3:])

    def test_failure_preserves_the_other_channel_and_requires_preprocessing_replay(self):
        self.track("microphone", 5)
        self.track("system", 5)
        self.progress()
        def fail_system(source, *args):
            if source == "system":
                raise ValueError("synthetic_model_interruption")
            return self.recognize(source, *args)
        failed = self.processor(fail_system)
        with self.assertRaisesRegex(ValueError, "synthetic_model_interruption"):
            failed.poll()
        with self.assertRaisesRegex(ValueError, "incremental_restart_required"):
            failed.poll()
        self.assertEqual(len(self.journal.events()), 1)
        recovered = self.processor()
        self.assertEqual(recovered.poll()["reused_sources"], 1)
        self.assertEqual(len(self.journal.events()), 2)
        self.assertEqual([r[0] for r in self.calls], ["microphone", "system"])

    def test_replay_does_not_expose_future_cached_blocks_before_validating_their_audio(self):
        self.track("system", 25); self.progress(); self.drain(self.processor())
        with (self.root/"sources/system.wav.part").open("r+b") as audio:
            audio.seek(44+20*RATE*2); audio.write(b"\0\0"*100)
        replay = self.processor()
        replay.poll()
        self.assertEqual(len(self.journal.events(through_frame=replay.position)), 1)
        with self.assertRaisesRegex(ValueError, "incremental_block_source_changed"):
            replay.poll()
        self.assertEqual(len(self.journal.events(through_frame=replay.position)), 1)

    def test_changed_committed_audio_cannot_reuse_or_overwrite_a_transcript(self):
        self.track("microphone", 3)
        self.progress()
        self.drain(self.processor())
        original = self.journal.events()
        with (self.root/"sources/microphone.wav.part").open("r+b") as audio:
            audio.seek(44+2000); audio.write(b"\0\0"*100)
        with self.assertRaisesRegex(ValueError, "incremental_block_source_changed"):
            self.processor().poll()
        self.assertEqual(self.journal.events(), original)

    def test_closed_shorter_source_is_padded_without_truncating_longer_source(self):
        self.track("microphone", 1)
        self.track("system", 25)
        self.progress()
        self.drain(self.processor())
        self.assertEqual([c[2] for c in self.calls], [12, 12, 24, 24, 25, 25])
        self.assertEqual([c[4] for c in self.calls if c[0] == "microphone"][1:], [0, 0])

    def test_changed_model_or_context_policy_cannot_reuse_the_journal(self):
        self.track("system", 1); self.progress(); self.drain(self.processor())
        with self.assertRaisesRegex(ValueError, "incremental_journal_policy_mismatch"):
            BlockJournal(self.root/"state/blocks.sqlite3", self.capture, "different-model")

    def test_source_rename_during_close_keeps_the_committed_prefix_readable(self):
        self.track("microphone", 13)
        self.progress("open")
        path = self.root/"sources/microphone.wav.part"
        path.rename(path.with_suffix(""))
        processor = self.processor()
        self.assertEqual(processor.poll()["state"], "processed")
        self.progress()
        self.assertEqual(processor.poll()["state"], "processed")
        self.assertEqual(processor.poll()["state"], "drained")

    def test_backwards_watermark_and_changed_capture_are_refused(self):
        self.track("system", 3); self.progress("open")
        reader = SourceReader(self.root); reader.progress()
        self.progress("open", {"system": RATE})
        with self.assertRaisesRegex(ValueError, "incremental_progress_invalid"):
            reader.progress()
        self.capture = str(uuid4()); self.progress()
        with self.assertRaisesRegex(ValueError, "incremental_progress_invalid"):
            reader.progress()

    def test_truncated_and_symlinked_sources_never_reach_inference(self):
        self.track("system", 3); self.progress()
        path = self.root/"sources/system.wav.part"
        with path.open("r+b") as audio:
            audio.truncate(44+RATE)
        with self.assertRaisesRegex(ValueError, "incremental_source_invalid"):
            self.processor().poll()
        path.unlink(); path.symlink_to(self.root/"sources/progress.json")
        with self.assertRaises(OSError):
            self.processor().poll()
        self.assertEqual(self.calls, [])

    def test_resampling_keeps_boundaries_and_filters_aliasing(self):
        signal = (.2*np.sin(2*np.pi*1000*np.arange(25*RATE)/RATE)).astype("float32")
        self.track("system", 25, signal); self.progress()
        reader = SourceReader(self.root); progress = reader.progress()
        first, _ = reader.samples("system", 0, 12*RATE, progress)
        second, _ = reader.samples("system", 12*RATE, 25*RATE, progress)
        whole, _ = reader.samples("system", 0, 25*RATE, progress)
        np.testing.assert_allclose(np.concatenate([first, second]), whole, atol=1e-7)
        high = (.2*np.sin(2*np.pi*12000*np.arange(RATE)/RATE)).astype("float32")
        self.track("system", 1, high); self.progress()
        reader = SourceReader(self.root); progress = reader.progress()
        filtered, _ = reader.samples("system", 0, RATE, progress)
        self.assertLess(float(np.sqrt(np.mean(filtered[100:-100]**2))), .001)


if __name__ == "__main__":
    unittest.main()
