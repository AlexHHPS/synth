import json
import hashlib
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from synth.server import audio_setup, desktop_host


class FirstRunAudioTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.previous=dict(audio_setup._STATE)
        self.addCleanup(lambda:audio_setup._STATE.update(self.previous))

    def test_clean_installation_is_blocked_until_automatic_activation_finishes(self):
        started,finish=threading.Event(),threading.Event();done=False
        def install(argv,on_progress,allow_queued):
            nonlocal done
            self.assertIn('--activate',argv);self.assertTrue(allow_queued)
            on_progress(2,'Descargando los modelos');started.set()
            if not finish.wait(3):raise AssertionError('Test timeout')
            done=True;return 0
        installer=SimpleNamespace(check_installation=lambda root:{'ready':done},main=install)
        with patch.object(audio_setup,'load_installer',return_value=installer):
            audio_setup.start(self.root)
            try:
                self.assertTrue(started.wait(3));self.assertFalse(audio_setup.ready())
                self.assertEqual(audio_setup.status()['step'],2)
            finally:finish.set();audio_setup._THREAD.join(3)
        self.assertTrue(audio_setup.ready())

    def test_failed_download_keeps_capture_blocked_and_retry_can_complete(self):
        installer=SimpleNamespace(check_installation=lambda root:{'ready':False},main=Mock(side_effect=OSError('Private network detail')))
        with patch.object(audio_setup,'load_installer',return_value=installer):
            audio_setup.start(self.root);audio_setup._THREAD.join(3)
        self.assertEqual(audio_setup.status()['state'],'failed')
        self.assertNotIn('Private',audio_setup.status()['message'])
        installer.check_installation=lambda root:{'ready':True}
        with patch.object(audio_setup,'load_installer',return_value=installer):
            audio_setup.start(self.root);audio_setup._THREAD.join(3)
        self.assertTrue(audio_setup.ready())

    def test_previous_whisper_configuration_is_migrated_to_qwen(self):
        (self.root/'asr-config.json').write_text(json.dumps({'backend':'whisper'}))
        installer=SimpleNamespace(check_installation=Mock(side_effect=[{'ready':False},{'ready':True}]),main=Mock(return_value=0))
        with patch.object(audio_setup,'load_installer',return_value=installer):
            audio_setup.start(self.root);audio_setup._THREAD.join(3)
        self.assertTrue(audio_setup.ready());installer.main.assert_called_once()

    def test_worker_does_not_claim_recordings_before_audio_is_ready(self):
        stop=threading.Event()
        with patch.object(desktop_host,'STOP',stop),patch.object(stop,'wait',side_effect=lambda seconds:stop.set()), \
             patch.object(audio_setup,'ready',return_value=False),patch.object(desktop_host,'work_once') as infer:
            desktop_host.worker()
        infer.assert_not_called()
        self.assertEqual(desktop_host.WORKER_STATUS['state'],'preparing_audio')

    def test_prepared_audio_never_starts_the_installer_again(self):
        installer=SimpleNamespace(check_installation=lambda root:{'ready':True},main=Mock())
        with patch.object(audio_setup,'load_installer',return_value=installer):
            audio_setup.start(self.root);audio_setup._THREAD.join(3)
        self.assertTrue(audio_setup.ready());installer.main.assert_not_called()


class ExistingModelReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.installer=audio_setup.load_installer()
        self.models={role:{'repo':'public/model','revision':'pinned','files':[{'path':'weights.bin','size':5,'sha256':hashlib.sha256(b'valid').hexdigest()}]} for role in ['asr','aligner']}
        self.targets,_=self.installer.model_download_plan(self.root,self.models)
        for target in self.targets.values():
            target.mkdir(parents=True);(target/'weights.bin').write_bytes(b'valid')
        runtime=self.root/'runtime/qwen-asr/bin';runtime.mkdir(parents=True);(runtime/'python').touch()
        self.lock=self.root/'model-lock.json';self.lock.write_text(json.dumps({'models':self.models}))

    def test_valid_models_are_reused_without_config_cache_or_network(self):
        from synth.worker import subprocesses
        before={role:(target/'weights.bin').stat() for role,target in self.targets.items()}
        with patch.object(self.installer,'LOCK',self.lock),patch.object(self.installer.platform,'system',return_value='Darwin'), \
             patch.object(self.installer.platform,'machine',return_value='arm64'),patch.object(self.installer.platform,'mac_ver',return_value=('14.2',(),'')), \
             patch.object(self.installer.shutil,'which',return_value='/bundled/uv'),patch.object(self.installer,'runtime_ready',return_value=True), \
             patch.object(self.installer.shutil,'disk_usage',return_value=SimpleNamespace(free=10**12)), \
             patch.object(subprocesses,'run_process',side_effect=AssertionError('Network or installer must not run')) as process:
            self.assertEqual(self.installer.main(['--activate','--app-data',str(self.root)]),0)
        process.assert_not_called()
        self.assertEqual(json.loads((self.root/'asr-config.json').read_text())['backend'],'qwen3')
        for role,target in self.targets.items():
            after=(target/'weights.bin').stat()
            self.assertEqual((before[role].st_ino,before[role].st_mtime_ns),(after.st_ino,after.st_mtime_ns))

    def test_only_missing_or_corrupt_files_are_selected_for_download(self):
        target=self.targets['asr'];(target/'weights.bin').write_bytes(b'wrong')
        _,missing=self.installer.model_download_plan(self.root,self.models)
        self.assertEqual(list(missing),['asr'])
        self.assertEqual([item['path'] for item in missing['asr']['files']],['weights.bin'])

    def test_insufficient_disk_space_fails_before_any_download(self):
        from synth.worker import subprocesses
        with patch.object(self.installer,'LOCK',self.lock),patch.object(self.installer.platform,'system',return_value='Darwin'), \
             patch.object(self.installer.platform,'machine',return_value='arm64'),patch.object(self.installer.platform,'mac_ver',return_value=('14.2',(),'')), \
             patch.object(self.installer.shutil,'which',return_value='/bundled/uv'),patch.object(self.installer,'runtime_ready',return_value=True), \
             patch.object(self.installer.shutil,'disk_usage',return_value=SimpleNamespace(free=1)),patch.object(subprocesses,'run_process') as process:
            with self.assertRaisesRegex(ValueError,'Libera al menos'):
                self.installer.main(['--activate','--app-data',str(self.root)])
        process.assert_not_called()
