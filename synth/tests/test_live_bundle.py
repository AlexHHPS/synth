"""Exercise the actual allowlisted source tree under -I, with no host importer."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from synth.worker.live_bundle import FILES, MANIFEST, data_arguments, describe, verify


class LiveBundleTests(unittest.TestCase):
    def test_isolated_imports_only_need_the_files_copied_by_the_packager(self):
        original = Path(__file__).resolve().parents[1]/'worker'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = root/'synth/worker'
            worker.mkdir(parents=True)
            manifest = root/MANIFEST
            manifest.write_text(json.dumps(describe(original)))
            arguments = data_arguments(original, manifest)
            for index in range(0, len(arguments), 2):
                self.assertEqual(arguments[index], '--add-data')
                source, destination = arguments[index+1].rsplit(':', 1)
                self.assertEqual(destination, 'synth/worker')
                shutil.copyfile(source, worker/Path(source).name)
            code = '''
import importlib, pathlib, sys
sys.path.insert(0, sys.argv[1])
from synth.worker.live_bundle import FILES, verify
verify(pathlib.Path(sys.argv[1])/'synth/worker')
for name in FILES:
    if name.endswith('.py'):
        importlib.import_module('synth.worker.'+name[:-3])
assert 'pydantic' not in sys.modules
assert 'mlx.core' not in sys.modules
print('ISOLATED_LIVE_BUNDLE_IMPORTS_PASS')
'''
            result = subprocess.run([sys.executable, '-I', '-c', code, str(root)], cwd=root,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'ISOLATED_LIVE_BUNDLE_IMPORTS_PASS')
            self.assertEqual(verify(worker)['protocol_version'], 3)
            (worker/'word_seams.py').write_text('# changed after packaging')
            with self.assertRaisesRegex(ValueError, 'qwen_live_bundle_changed'):
                verify(worker)
            (worker/'contextual_audio.py').unlink()
            with self.assertRaisesRegex(ValueError, 'qwen_live_bundle_missing'):
                verify(worker)
