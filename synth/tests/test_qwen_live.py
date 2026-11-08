import fcntl
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from synth.worker import qwen_live_runner as live


class LiveQwenProtocolTests(unittest.TestCase):
    def test_audio_is_finite_mono_and_bounded(self):
        self.assertEqual(len(live.validate_samples([0.01]*1600)),1600)
        for samples in [[],[0.0]*1599,[0.0]*(live.MAX_SAMPLES+1),[float('nan')]*1600,[float('inf')]*1600,[True]*1600,[1.1]*1600]:
            with self.subTest(length=len(samples)):
                with self.assertRaisesRegex(ValueError,'qwen_live_audio_invalid'):
                    live.validate_samples(samples)

    def test_live_start_refuses_a_busy_acoustic_lease_before_loading_any_model(self):
        with tempfile.TemporaryDirectory() as directory:
            with (Path(directory)/'acoustic-worker.lock').open('w') as lease:
                fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
                result=subprocess.run([sys.executable,'-I',live.__file__],input=json.dumps({'command':'load','model':'unused','state':directory})+'\n',text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,1)
            self.assertEqual(json.loads(result.stdout),{'error_code':'acoustic_worker_already_running'})

    def test_invalid_initial_request_cannot_open_files_or_load_a_model(self):
        result=subprocess.run([sys.executable,'-I',live.__file__],input='{"samples":[0]}\n',text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,1)
        self.assertEqual(json.loads(result.stdout),{'error_code':'qwen_live_request_invalid'})
