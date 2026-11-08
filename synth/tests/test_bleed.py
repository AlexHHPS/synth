import unittest

import numpy as np

from synth.worker import bleed

RATE = 16000


def tone(freq, seconds, amplitude=0.3, seed=0):
    t = np.arange(int(RATE * seconds)) / RATE
    noise = np.random.default_rng(seed).normal(0, 0.05, len(t))
    # Speech-like: a carrier modulated by a 4 Hz syllable envelope.
    return (amplitude * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t)) * (np.sin(2 * np.pi * freq * t) + noise)).astype("float32")


def rms(signal):
    return float(np.sqrt(np.mean(signal.astype("float64") ** 2)) + 1e-12)


class AcousticGateTests(unittest.TestCase):
    def setUp(self):
        silence = np.zeros(RATE * 4, "float32")
        self.remote = np.concatenate([tone(220, 4), silence, tone(260, 4, seed=1), silence])
        self.local = np.concatenate([silence, tone(330, 4, seed=2), silence, tone(300, 4, seed=3)])
        delay = int(0.08 * RATE)
        echo = np.concatenate([np.zeros(delay, "float32"), self.remote])[: len(self.remote)] * 10 ** (-18 / 20)
        self.mic = self.local + echo + np.random.default_rng(9).normal(0, 0.001, len(self.local)).astype("float32")

    def test_echo_only_frames_are_muted(self):
        gated, stats = bleed.gate_microphone(self.mic, self.remote)
        remote_only = slice(int(0.5 * RATE), int(3.5 * RATE))
        self.assertLess(rms(gated[remote_only]), rms(self.mic[remote_only]) / 5)
        self.assertAlmostEqual(stats["lag_ms"], 80, delta=10)
        self.assertGreater(stats["gated_fraction"], 0.2)

    def test_local_speech_is_kept(self):
        gated, _ = bleed.gate_microphone(self.mic, self.remote)
        local_only = slice(int(4.5 * RATE), int(7.5 * RATE))
        self.assertGreater(rms(gated[local_only]), rms(self.mic[local_only]) * 0.9)

    def test_without_system_audio_the_mic_is_untouched(self):
        gated, stats = bleed.gate_microphone(self.local, np.zeros_like(self.local))
        self.assertTrue(np.array_equal(gated, self.local))
        self.assertEqual(stats["gated_fraction"], 0.0)


class EchoWordTests(unittest.TestCase):
    def words(self, text, start, step=300):
        return [{"text": w, "start_ms": start + i * step, "end_ms": start + i * step + 250}
                for i, w in enumerate(text.split())]

    def test_real_case_t492_remote_phrase_fragmented_on_the_microphone(self):
        # Synth workshop, t=492 s: the remote phrase is whole on `system` and fragmented on `microphone`.
        system = self.words("¿Quieres que preparemos la apertura de una ronda de financiación?", 492_000)
        mic = self.words("quieres que preparemos", 492_100) + self.words("la apertura de una ronda", 493_200)
        kept, removed = bleed.drop_echo_words(mic, system)
        self.assertEqual(kept, [])
        self.assertEqual(removed, 8)

    def test_local_answer_after_the_question_is_kept(self):
        system = self.words("¿Quieres que preparemos la apertura de una ronda?", 492_000)
        mic = self.words("sí la preparo yo esta semana", 495_000)
        kept, removed = bleed.drop_echo_words(mic, system)
        self.assertEqual(removed, 0)
        self.assertEqual(len(kept), 6)

    def test_same_words_far_away_are_not_echo(self):
        system = self.words("vamos a revisar el presupuesto", 10_000)
        mic = self.words("vamos a revisar el presupuesto", 300_000)
        self.assertEqual(bleed.drop_echo_words(mic, system)[1], 0)


if __name__ == "__main__":
    unittest.main()
