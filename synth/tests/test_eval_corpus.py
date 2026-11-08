import io
import random
import unittest

import numpy as np

try:
    import soundfile
    from synth.eval import corpus
except ImportError:  # the slim API image has no audio stack
    soundfile = None


def clip(freq, seconds=1.0):
    t = np.arange(int(16000 * seconds)) / 16000
    buffer = io.BytesIO()
    soundfile.write(buffer, (0.3 * np.sin(2 * np.pi * freq * t)).astype("float32"), 16000, format="WAV")
    return buffer.getvalue()


def pools():
    return {s: [{"audio_id": f"{s}-{i}", "raw_text": f"frase {i} de {s}", "audio": {"bytes": clip(200 + 100 * k)}}
                for i in range(12)] for k, s in enumerate(["op", "g1", "g2"])}


@unittest.skipIf(soundfile is None, "audio stack not installed")
class SyntheticMeetingTests(unittest.TestCase):
    def build(self, in_person=False):
        spec = corpus.MeetingSpec("t", 2, 40, in_person=in_person, echo_delay_ms=80, echo_db=-18)
        return corpus.build_meeting(spec, "op", ["g1", "g2"], pools(), random.Random(7))

    def test_build_is_deterministic(self):
        first, ref_first = self.build()
        second, ref_second = self.build()
        self.assertEqual(ref_first, ref_second)
        self.assertTrue(np.array_equal(first["system"], second["system"]))

    def test_turns_are_ordered_and_on_the_right_channel(self):
        _, reference = self.build()
        starts = [t["start_ms"] for t in reference["turns"]]
        self.assertEqual(starts, sorted(starts))
        for turn in reference["turns"]:
            self.assertEqual(turn["channel"], "microphone" if turn["speaker"] == "op" else "system")
            self.assertLess(turn["start_ms"], turn["end_ms"])
        self.assertEqual(reference["speakers_per_channel"]["system"], 2)

    def test_microphone_contains_system_echo(self):
        signals, reference = self.build()
        remote = next(t for t in reference["turns"] if t["channel"] == "system")
        lo, hi = remote["start_ms"] * 16 + 2000, remote["end_ms"] * 16 - 2000
        mic, system = signals["microphone"][lo:hi], signals["system"][lo:hi]
        self.assertGreater(np.sqrt(np.mean(mic ** 2)), 0.01)
        self.assertLess(np.sqrt(np.mean(mic ** 2)), np.sqrt(np.mean(system ** 2)) / 4)

    def test_in_person_meeting_is_microphone_only(self):
        signals, reference = self.build(in_person=True)
        self.assertEqual(set(signals), {"microphone"})
        self.assertEqual(reference["speakers_per_channel"]["microphone"], 3)
        self.assertGreater(reference["operator_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
