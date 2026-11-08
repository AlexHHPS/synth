import hashlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('host_entry_test', Path(__file__).parents[1]/'desktop_entry.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class HostUpgradeTests(unittest.TestCase):
    def run_upgrade(self, running_hash, task_state=None):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            app = home/'Library/Application Support/dev.synth.voice'
            app.mkdir(parents=True)
            binary = home/'synth-voice-host'; binary.write_bytes(b'new-host-fixture')
            (app/'desktop-api-key').write_text('fixture')
            resources = home/'resources'; resources.mkdir()
            (resources/'host-build.json').write_text(json.dumps({'id': 'same-build'}))
            agent = home/'Library/LaunchAgents/dev.synth.voice.pipeline.plist'
            agent.parent.mkdir(parents=True)
            agent.write_bytes(plistlib.dumps({'ProgramArguments': [str(binary)]}))
            if task_state:
                (app/'pipeline').mkdir()
                with sqlite3.connect(app/'pipeline/queue.sqlite3') as db:
                    db.execute('CREATE TABLE desktop_tasks(state TEXT)')
                    db.execute('INSERT INTO desktop_tasks VALUES (?)', (task_state,))
            response = io.BytesIO(json.dumps({'host_build': 'same-build', 'host_binary_sha256': running_hash}).encode())
            with patch.object(Path, 'home', return_value=home), \
                 patch.object(entry.sys, '_MEIPASS', str(resources), create=True), \
                 patch.object(entry.sys, 'executable', str(binary)), \
                 patch('urllib.request.urlopen', return_value=response), \
                 patch.object(entry.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as calls:
                entry.install_agent()
            return [c.args[0][1] for c in calls.call_args_list]

    def test_same_build_but_changed_binary_restarts_stale_frozen_process(self):
        self.assertEqual(self.run_upgrade('old-binary'), ['bootout', 'bootstrap'])

    def test_old_host_without_fingerprint_is_not_reused(self):
        self.assertEqual(self.run_upgrade(None), ['bootout', 'bootstrap'])

    def test_matching_binary_does_not_restart(self):
        fingerprint = hashlib.sha256(b'new-host-fixture').hexdigest()
        self.assertEqual(self.run_upgrade(fingerprint), [])

    def test_queued_checkpoint_can_resume_with_new_host(self):
        self.assertEqual(self.run_upgrade('old-binary', 'queued'), ['bootout', 'bootstrap'])

    def test_live_processing_still_blocks_host_replacement(self):
        with self.assertRaises(SystemExit):
            self.run_upgrade('old-binary', 'running')


if __name__ == '__main__':
    unittest.main()
