import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from synth.server.capture_owner import validate_sources
from synth.worker.desktop_queue import QueueError


class CaptureOwnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.capture = self.root / 'meeting'
        (self.capture / 'sources').mkdir(parents=True)
        self.source = self.capture / 'sources/microphone.wav'
        self.source.write_bytes(b'public fixture placeholder')
        self.capture_id = str(uuid4())
        (self.capture / 'sources/progress.json').write_text(json.dumps({'capture_id': self.capture_id}))
        self.owner = self.capture / 'capture-owner.json'
        self.owner.write_text(json.dumps({'version': 1, 'capture_id': self.capture_id, 'identity': 'user-a'}))

    def validate(self, identity='user-a', capture_id=None, sources=None):
        validate_sources(sources or {'microphone': str(self.source)}, capture_id or self.capture_id, identity, self.root)

    def test_new_capture_cannot_be_claimed_by_another_user_or_identifier(self):
        self.validate()
        with self.assertRaisesRegex(QueueError, 'capture_owner_mismatch'):
            self.validate(identity='user-b')
        with self.assertRaisesRegex(QueueError, 'capture_owner_mismatch'):
            self.validate(capture_id=str(uuid4()))

    def test_missing_corrupt_and_symlink_owner_are_rejected(self):
        self.owner.unlink()
        with self.assertRaisesRegex(QueueError, 'capture_owner_missing'):
            self.validate()
        self.owner.write_text('not json')
        with self.assertRaisesRegex(QueueError, 'capture_owner_invalid'):
            self.validate()
        other = self.root / 'owner.json'
        self.owner.rename(other)
        self.owner.symlink_to(other)
        with self.assertRaisesRegex(QueueError, 'capture_owner_missing'):
            self.validate()

    def test_sources_cannot_mix_two_captures(self):
        other = self.root / 'other'
        other.mkdir()
        (other / 'system.wav').write_bytes(b'public fixture placeholder')
        with self.assertRaisesRegex(QueueError, 'capture_sources_different_sessions'):
            self.validate(sources={'microphone': str(self.source), 'system': str(other / 'system.wav')})

    def test_outside_source_and_its_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / 'source.wav'
            outside.write_bytes(b'public fixture placeholder')
            link = self.capture / 'sources/link.wav'
            link.symlink_to(outside)
            for path in [outside, link]:
                with self.assertRaisesRegex(QueueError, 'capture_path_not_owned'):
                    self.validate(sources={'microphone': str(path)})

    def test_legacy_capture_remains_supported_without_fabricating_an_owner(self):
        (self.capture / 'sources/progress.json').unlink()
        self.owner.unlink()
        self.validate()


if __name__ == '__main__':
    unittest.main()
