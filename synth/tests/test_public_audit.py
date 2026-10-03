import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('public_audit', Path(__file__).resolve().parents[2]/'scripts/audit-public.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class PublicAuditTests(unittest.TestCase):
    def test_staged_internal_content_is_detected_even_after_working_file_is_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = audit.ROOT
            audit.ROOT = root
            try:
                subprocess.run(['git', 'init', '-q', str(root)], check=True)
                file = root/'config.txt'
                file.write_text('https://api.\x73apira.ai')
                subprocess.run(['git', '-C', str(root), 'add', 'config.txt'], check=True)
                file.unlink()
                self.assertFalse(audit.audit('working')['violations'])
                self.assertTrue(audit.audit('index')['violations'])
                file.write_text('https://voice.example.com')
                subprocess.run(['git', '-C', str(root), 'add', 'config.txt'], check=True)
                self.assertFalse(audit.audit('index')['violations'])
            finally:
                audit.ROOT = old
