import fcntl
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
import time

from synth.worker import qwen_live_runner as live


class LiveQwenProtocolTests(unittest.TestCase):
    def test_capture_protocol_reads_its_identity_before_loading_private_vocabulary(self):
        """Exercise load -> capture, including the real owner and progress checks."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder, state = root/'capture', root/'state'
            (folder/'sources').mkdir(parents=True)
            state.mkdir()
            capture, owner = str(uuid4()), str(uuid4())
            (folder/'sources/progress.json').write_text(json.dumps({
                'version': 1, 'capture_id': capture, 'state': 'open',
                'clock': 'recording_elapsed', 'sample_format': 'pcm16',
                'sample_rate': 48000, 'sources': {'microphone': 0}}))
            (folder/'capture-owner.json').write_text(json.dumps({
                'version': 1, 'capture_id': capture, 'identity': owner}))
            (state/'prepared-capture-context.json').write_text(json.dumps({
                'owner': owner, 'created_at': time.time(), 'terms': ['Synth']}))
            runtime = json.loads(Path(live.__file__).with_name('qwen-model-lock.json').read_text())['runtime']
            requests = [
                {'command': 'load', 'state': str(state), 'model': str(root/'model')},
                {'command': 'capture', 'folder': str(folder), 'echo_cancellation': 'off'},
                {'command': 'stop'},
            ]
            mx, model = Mock(), Mock()
            with patch.object(live, 'read_request', side_effect=requests), \
                 patch.object(live, 'send') as send, \
                 patch.object(live, 'validate_model'), \
                 patch.object(live.importlib.metadata, 'version', side_effect=runtime.__getitem__), \
                 patch.dict(sys.modules, {'mlx': Mock(core=mx), 'mlx.core': mx,
                     'mlx_audio': Mock(), 'mlx_audio.stt': Mock(),
                     'mlx_audio.stt.utils': Mock(load_model=Mock(return_value=model))}):
                live.main()
            self.assertTrue(send.call_args_list[0].args[0]['ready'])
            self.assertEqual(send.call_args_list[1].args[0]['capture_id'], capture)
            self.assertTrue(send.call_args_list[1].args[0]['attached'])
            vocabulary = json.loads((folder/'asr-vocabulary.json').read_text())
            self.assertEqual(vocabulary, {'owner': owner, 'capture_id': capture, 'terms': ['Synth']})
            model.generate.assert_not_called()

    def test_audio_is_finite_mono_and_bounded(self):
        self.assertEqual(len(live.validate_samples([0.01]*1600)),1600)
        for samples in [[],[0.0]*1599,[0.0]*(live.MAX_SAMPLES+1),[float('nan')]*1600,[float('inf')]*1600,[True]*1600,[1.1]*1600]:
            with self.subTest(length=len(samples)):
                with self.assertRaisesRegex(ValueError,'qwen_live_audio_invalid'):
                    live.validate_samples(samples)

    def test_a_window_that_exhausts_its_budget_is_halved_and_only_the_looping_part_is_withheld(self):
        calls=[]
        def generate(clip):
            calls.append(len(clip))
            # Anything containing the first second loops; the rest decodes.
            looping=clip[0]=='loop'
            return ('' if looping else f'ok{len(clip)//16000}', 0 if looping else 5, looping)
        audio=['loop']+['x']*(18*16000-1)
        text,tokens,incomplete=live.decode_within_budget(generate,audio)
        self.assertTrue(incomplete)
        self.assertEqual(text,'ok4 ok9')
        self.assertEqual(tokens,10)
        self.assertEqual(calls,[18*16000,9*16000,72000,72000,9*16000])
        self.assertEqual(live.decode_within_budget(lambda clip:('todo',7,False),audio),('todo',7,False))

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
