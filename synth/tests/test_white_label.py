import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class WhiteLabelTests(unittest.TestCase):
    def test_second_brand_updates_metadata_namespace_and_native_destination_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = ['branding.json', 'scripts/configure-brand.py', 'synth/config.py', 'synth/deployment.json',
                     'frontend/src-tauri/tauri.conf.json', 'frontend/src/synth/design-system/tokens.css',
                     'frontend/public/example-mark.svg', 'frontend/public/synth-mark.png']
            for name in files:
                destination = root/name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT/name, destination)
            (root/'frontend/src/app').mkdir(parents=True)
            namespace = root/'synth/namespace.py'
            namespace.write_text("IDENTIFIER = 'dev.synth.voice'\n")
            command = [sys.executable, str(root/'scripts/configure-brand.py'), '--name', 'Example Voice',
                       '--identifier', 'dev.example.voice', '--accent', '160 70% 35%',
                       '--logo', '/example-mark.svg', '--api-url', 'https://voice.example.com',
                       '--supabase-url', 'https://exampleproject.supabase.co']
            subprocess.run(command, check=True, capture_output=True)
            brand = json.loads((root/'branding.json').read_text())
            native = json.loads((root/'frontend/src-tauri/tauri.conf.json').read_text())
            deploy = json.loads((root/'synth/deployment.json').read_text())
            self.assertEqual(brand['name'], native['productName'])
            self.assertEqual(brand['identifier'], native['identifier'])
            self.assertIn(brand['identifier'], namespace.read_text())
            self.assertEqual(brand['logo'], '/example-mark.svg')
            self.assertEqual(deploy['auth_mode'], 'supabase')
            self.assertIn(deploy['api_url'], (root/'.cargo/config.toml').read_text())
            self.assertIn('--brand: 160 70% 35%;', (root/'frontend/src/synth/design-system/tokens.css').read_text())
            rejected = command.copy()
            rejected[rejected.index('--api-url')+1] = 'https://voice.example.com@evil.invalid'
            result = subprocess.run(rejected, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads((root/'synth/deployment.json').read_text()), deploy)
