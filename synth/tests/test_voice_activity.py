"""Real local presence estimator: uncertainty and state, not calibration."""
import unittest
import numpy as np
from synth.worker.voice_activity import SpeechPresence


class VoiceActivityTests(unittest.TestCase):
    def test_cold_uncertainty_never_rejects_a_possible_short_first_utterance(self):
        detector = SpeechPresence()
        first = detector.analyze("microphone", np.zeros(192000, "float32"))
        self.assertIsNone(first["has_speech"])
        self.assertEqual(first["state"], "uncertain_start")
        second = detector.analyze("microphone", np.zeros(192000, "float32"))
        self.assertIs(second["has_speech"], False)
        self.assertEqual(second["state"], "no_speech")

    def test_channels_and_captures_do_not_share_detector_state(self):
        detector = SpeechPresence()
        detector.analyze("system", np.zeros(192000, "float32"))
        detector.analyze("system", np.zeros(192000, "float32"))
        self.assertIsNone(detector.analyze("microphone", np.zeros(192000, "float32"))["has_speech"])
        self.assertIsNone(SpeechPresence().analyze("system", np.zeros(192000, "float32"))["has_speech"])

    def test_replaying_the_same_prefix_reconstructs_the_same_presence_state(self):
        signal = np.random.default_rng(61).normal(0, .0015, 25*16000).astype("float32")
        outcomes = []
        for _ in range(2):
            detector = SpeechPresence()
            outcomes.append([detector.analyze("microphone", signal[i:i+192000]) for i in range(0,len(signal),192000)])
        self.assertEqual(outcomes[0], outcomes[1])

    def test_invalid_input_is_not_classified_as_silence(self):
        detector = SpeechPresence()
        for signal in (np.zeros(0,"float32"), np.zeros((1,160),"float32"), np.full(160,np.nan,"float32")):
            with self.assertRaisesRegex(ValueError, "incremental_voice_activity_input_invalid"):
                detector.analyze("microphone", signal)


if __name__ == "__main__":
    unittest.main()
