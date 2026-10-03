import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError
from synth.server.voice_registry import Enrollment, seal, unseal
from synth.speakers import keychain
from synth.worker.desktop_pipeline import delete_processed_audio


class VoiceSecurityTests(unittest.TestCase):
    def test_centroid_is_encrypted_and_bound_to_owner_and_model(self):
        with patch.dict(os.environ, {"VOICE_PROFILE_ENCRYPTION_KEY": base64.b64encode(b"k"*32).decode()}):
            body=Enrollment(id=uuid4(),model="m",embedding=[1]+[0]*255,consent_confirmed=True)
            owner=uuid4(); nonce, ciphertext=seal(body,owner)
            row={"id":body.id,"owner_id":owner,"model":"m","nonce":nonce,"ciphertext":ciphertext}
            self.assertEqual(unseal(row),body.embedding)
            self.assertNotIn(json.dumps(body.embedding).encode(),ciphertext)
            with self.assertRaises(InvalidTag): unseal({**row,"owner_id":uuid4()})
            with self.assertRaises(InvalidTag): unseal({**row,"model":"other"})

    def test_audio_fields_and_invalid_embeddings_are_rejected(self):
        valid={"id":str(uuid4()),"model":"m","embedding":[1]+[0]*255,"consent_confirmed":True}
        for changed in [{"audio":"RIFF"},{"recording_url":"https://example.invalid/a.wav"},{"embedding":[0]*256},{"embedding":[float("nan")]+[0]*255},{"embedding":[1]*255}]:
            with self.assertRaises(ValidationError): Enrollment.model_validate({**valid,**changed})

    def test_key_migration_does_not_delete_before_verified_keychain_write(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"vault-key"; p.write_text((b"k"*32).hex())
            with patch.object(keychain,"read",return_value=None),patch.object(keychain,"write",side_effect=ValueError("denied")):
                with self.assertRaises(ValueError): keychain.obtain(p)
            self.assertTrue(p.exists())
            with patch.object(keychain,"read",return_value=b"k"*32):
                self.assertEqual(keychain.obtain(p),b"k"*32)
            self.assertFalse(p.exists())

    def test_audio_cleanup_preserves_text_other_captures_and_external_originals(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)/"raw"; root.mkdir(); captured=root/"own.wav"; captured.write_bytes(b"audio")
            unrelated=root/"other.wav"; unrelated.write_bytes(b"other")
            processed=root/"processed-task"; processed.mkdir()
            (processed/"microphone.wav").write_bytes(b"normalized")
            text=processed/"transcript.json"; text.write_text("{}")
            task={"id":"task","metadata":{"sources":{"microphone":{"path":str(captured)}}}}
            self.assertEqual(delete_processed_audio(task,root),2)
            self.assertTrue(unrelated.exists()); self.assertTrue(text.exists())
            outside=Path(t)/"original.wav"; outside.write_bytes(b"original")
            task["metadata"]["sources"]["microphone"]["path"]=str(outside)
            with self.assertRaises(ValueError): delete_processed_audio(task,root)
            self.assertTrue(outside.exists())


if __name__ == "__main__": unittest.main()
