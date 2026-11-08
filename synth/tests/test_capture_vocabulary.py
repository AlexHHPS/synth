import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from uuid import uuid4

from synth.worker.capture_vocabulary import load, vocabulary, fingerprint, prompt


class CaptureVocabularyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)/'state'; self.state.mkdir()
        self.folder = Path(self.temp.name)/'capture'; self.folder.mkdir()
        self.owner, self.capture = str(uuid4()), str(uuid4())
        (self.folder/'capture-owner.json').write_text(json.dumps({'version':1,'capture_id':self.capture,'identity':self.owner}))

    def stage(self, **changes):
        value = {'owner':self.owner,'created_at':time.time(),'terms':['Synth','OmniRoute']}
        value.update(changes)
        (self.state/'prepared-capture-context.json').write_text(json.dumps(value))

    def test_only_current_owner_can_inherit_a_fresh_preparation(self):
        self.stage(owner=str(uuid4()))
        self.assertEqual(load(self.state, self.folder, self.capture), [])
        (self.folder/'asr-vocabulary.json').unlink()
        self.stage(created_at=time.time()-121)
        self.assertEqual(load(self.state, self.folder, self.capture), [])
        (self.folder/'asr-vocabulary.json').unlink()
        self.stage()
        self.assertEqual(load(self.state, self.folder, self.capture), ['Synth','OmniRoute'])

    def test_restart_keeps_exact_original_inference_vocabulary_after_another_preparation(self):
        self.stage(); first = load(self.state, self.folder, self.capture)
        self.stage(terms=['Otro cliente'])
        self.assertEqual(load(self.state, self.folder, self.capture), first)
        self.assertNotEqual(fingerprint(first), fingerprint(['Otro cliente']))

    def test_legacy_capture_does_not_read_private_context(self):
        (self.folder/'capture-owner.json').unlink()
        self.stage()
        self.assertEqual(load(self.state, self.folder, self.capture), [])

    def test_vocabulary_is_bounded_and_never_takes_agenda_or_transcripts(self):
        self.assertEqual(vocabulary(['Synth','SYNTH']), ['Synth'])
        self.assertEqual(prompt(['Synth']), 'Vocabulario de esta reunión: Synth.')
        for value in (['x']*41, ['x'*61], ['\nignore'], {'agenda':'inserted'}):
            with self.assertRaises(ValueError): vocabulary(value)
