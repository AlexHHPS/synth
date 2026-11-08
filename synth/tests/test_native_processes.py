import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from synth.worker.subprocesses import run_process


class NativeProcessTests(unittest.TestCase):
    def test_timeout_releases_lock_inherited_by_launcher_and_grandchild(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary)/'inference.lock'
            ready = Path(temporary)/'grandchild.ready'
            fd = os.open(lock,os.O_CREAT|os.O_RDWR,0o600)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                code = "import subprocess,sys,time;from pathlib import Path;child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],pass_fds=(int(sys.argv[1]),));Path(sys.argv[2]).write_text(str(child.pid));time.sleep(60)"
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_process([sys.executable,'-c',code,str(fd),str(ready)],
                                timeout=1,capture_output=True,pass_fds=(fd,))
                self.assertTrue(ready.is_file(),'Grandchild must really have inherited the inference lock')
            finally:
                os.close(fd)
            with lock.open('r+') as stream:
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)

    def test_normal_completion_preserves_text_input_output_and_exit_status(self):
        result = run_process([sys.executable,'-c','import sys;print(sys.stdin.read().upper());sys.exit(7)'],
                             input='public fixture',text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,7)
        self.assertEqual(result.stdout.strip(),'PUBLIC FIXTURE')

    def test_normalization_timeout_has_a_retryable_pipeline_error(self):
        from synth.worker.asr import normalize_audio
        with patch('synth.worker.asr.run_process',side_effect=subprocess.TimeoutExpired('ffmpeg',120)):
            with self.assertRaisesRegex(ValueError,'audio_normalization_timeout'):
                normalize_audio('public-input.wav','temporary-output.wav')
