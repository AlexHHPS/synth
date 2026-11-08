"""Native audio-runtime checks: run with the isolated Qwen Python environment."""
import unittest
import importlib.util
try:
    import numpy as np
except ImportError:
    np = None
from synth.worker.echo import cancel_echo


@unittest.skipUnless(np is not None and importlib.util.find_spec('pywebrtc_audio'), 'Requires the isolated acoustic runtime')
class EchoAudioTests(unittest.TestCase):
    def test_no_system_reference_is_bit_exact(self):
        mic = np.random.default_rng(9).normal(0, .01, 16123).astype('float32')
        result, stats = cancel_echo(mic, np.zeros_like(mic))
        np.testing.assert_array_equal(result, mic)
        self.assertEqual(stats['skipped'], 'system_silent')

    def test_short_reference_does_not_truncate_the_microphone(self):
        mic = np.random.default_rng(3).normal(0, .01, 16123).astype('float32')
        far = np.random.default_rng(4).normal(0, .02, 8000).astype('float32')
        result, _ = cancel_echo(mic, far)
        self.assertEqual(result.shape, mic.shape)
        self.assertTrue(np.isfinite(result).all())

    def test_invalid_audio_is_rejected(self):
        for bad in [np.array([np.nan], 'float32'), np.zeros((2, 160), 'float32')]:
            with self.assertRaisesRegex(ValueError, 'echo_audio_invalid'):
                cancel_echo(bad, np.ones(160, 'float32'))


if __name__ == '__main__': unittest.main()
