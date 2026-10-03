import asyncio
import base64
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from synth.server.app import lifespan
from synth.server.voice_registry import cipher


class RuntimeStartupTests(unittest.TestCase):
    def test_runtime_mode_checks_existing_schema_without_ddl(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = {'relation': 'exists'}
        async def run():
            async with lifespan(None): pass
        with patch.dict(os.environ, {'SYNTH_RUN_MIGRATIONS': '0'}), patch('synth.server.app.migrate') as migrate, patch('synth.server.app.connect') as connect:
            connect.return_value.__enter__.return_value = db
            asyncio.run(run())
            migrate.assert_not_called()
            self.assertTrue(all('to_regclass' in call.args[0] for call in db.execute.call_args_list))

    def test_missing_schema_is_an_explicit_startup_failure(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = {'relation': None}
        async def run():
            async with lifespan(None): pass
        with patch.dict(os.environ, {'SYNTH_RUN_MIGRATIONS': '0'}), patch('synth.server.app.connect') as connect:
            connect.return_value.__enter__.return_value = db
            with self.assertRaisesRegex(ValueError, 'database_schema_initialization_required'):
                asyncio.run(run())

    def test_server_voice_key_can_be_read_from_a_secret_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'server-key'
            path.write_text(base64.b64encode(bytes(range(32))).decode())
            with patch.dict(os.environ, {'VOICE_PROFILE_ENCRYPTION_KEY_FILE': str(path)}):
                encrypted = cipher().encrypt(bytes(12), b'synthetic-centroid', b'identity')
                self.assertEqual(cipher().decrypt(bytes(12), encrypted, b'identity'), b'synthetic-centroid')
