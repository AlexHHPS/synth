"""Real installed AEC3, synthetic signals; not an echo-quality calibration."""
import unittest
import numpy as np
from pywebrtc_audio import AudioProcessor
from synth.worker.echo import StreamingEchoCanceller


class StreamingEchoTests(unittest.TestCase):
    def test_block_processing_matches_one_continuous_native_state(self):
        rate = 16000
        positions = np.arange(25*rate)
        far = (.08*np.sin(2*np.pi*220*positions/rate) + np.random.default_rng(617).normal(0, .015, len(positions))).astype("float32")
        mic = (.4*np.concatenate([np.zeros(640, "float32"), far[:-640]]) + .04*np.sin(2*np.pi*730*positions/rate)).astype("float32")
        whole = AudioProcessor(sample_rate=rate, echo_cancellation=True,
            noise_suppression=False, auto_gain_control=False, stream_delay_ms=0).process(mic, far)
        stream = StreamingEchoCanceller()
        blocks = [stream({"microphone": mic[i:i+12*rate], "system": far[i:i+12*rate]})["microphone"]
                  for i in range(0, len(mic), 12*rate)]
        np.testing.assert_allclose(np.concatenate(blocks), whole, atol=1e-6, rtol=0)

    def test_single_source_is_preserved_and_partial_tail_keeps_its_length(self):
        stream = StreamingEchoCanceller()
        mic = np.full(12345, .1, "float32")
        self.assertIs(stream({"microphone": mic})["microphone"], mic)
        result = stream({"microphone": mic, "system": np.zeros_like(mic)})
        self.assertEqual(len(result["microphone"]), len(mic))
        self.assertTrue(np.isfinite(result["microphone"]).all())


if __name__ == "__main__":
    unittest.main()
