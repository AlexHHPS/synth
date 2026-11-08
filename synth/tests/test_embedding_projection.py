import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from synth.speakers.native import project_embeddings, REVISION
from synth.speakers.matching import classify


class ProjectionTests(unittest.TestCase):
    def test_private_projection_batches_preserve_order_and_erase_intermediates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);binary=root/'native';binary.write_text('test')
            calls=[]
            def run(command, **kwargs):
                source,target=Path(command[2]),Path(command[4])
                values=json.loads(source.read_text());calls.append(len(values))
                self.assertEqual(stat.S_IMODE(source.stat().st_mode),0o600)
                self.assertEqual(stat.S_IMODE(source.parent.stat().st_mode),0o700)
                target.write_text(json.dumps({'mode':'real','revision':REVISION,'embedding_space':'plda_rho128_v1',
                                              'vectors':[[1.0]+[0.0]*127 for _ in values]}))
                return SimpleNamespace(returncode=0)
            with patch('synth.speakers.native.verify_models'), patch('synth.speakers.native.run_process',side_effect=run):
                vectors=project_embeddings([[1.0]+[0.0]*255]*129,root,binary=binary)
            self.assertEqual(calls,[128,1])
            self.assertEqual(len(vectors),129)
            self.assertEqual(len(vectors[0]),128)
            self.assertEqual(list(root.iterdir()),[binary])

    def test_invalid_projection_protocol_is_rejected_and_erased(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);binary=root/'native';binary.write_text('test')
            def run(command, **kwargs):
                Path(command[4]).write_text(json.dumps({'mode':'real','revision':REVISION,
                    'embedding_space':'different','vectors':[[1.0]+[0.0]*127]}))
                return SimpleNamespace(returncode=0)
            with patch('synth.speakers.native.verify_models'), patch('synth.speakers.native.run_process',side_effect=run):
                with self.assertRaisesRegex(ValueError,'speaker_projection_invalid'):
                    project_embeddings([[1.0]+[0.0]*255],root,binary=binary)
            self.assertEqual(list(root.iterdir()),[binary])

    def test_projected_space_is_explicit_and_still_never_asserts_identity(self):
        vector=[1.0]+[0.0]*127
        result=classify([vector,vector],[{'id':'p','employee_id':'alice','vector':vector}],dimensions=128)
        self.assertEqual(result['state'],'candidate')
        self.assertIsNone(result['employee_id'])
        with self.assertRaisesRegex(ValueError,'invalid_embedding'):
            classify([vector,vector],[{'id':'p','vector':vector}])
