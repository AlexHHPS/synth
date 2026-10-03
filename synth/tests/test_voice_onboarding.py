import json
from pathlib import Path
import tempfile
import unittest
from array import array
from unittest.mock import patch
from synth.speakers import onboarding
from synth.server.desktop_host import public_task

class OnboardingTests(unittest.TestCase):
    def test_quality_counts_clean_speech_and_rejects_clipping(self):
        good=onboarding.quality(array("h",[4000]*240000),[{"speaker_id":"s","start_ms":0,"end_ms":15000}])
        self.assertEqual(good["clean_speech_seconds"],15)
        self.assertFalse(good["overlap"])
        self.assertEqual(good["clipped_fraction"],0)
        self.assertGreater(good["rms_dbfs"],-40)
        bad=onboarding.quality(array("h",[32767]*48000),[{"start_ms":0,"end_ms":2000},{"start_ms":1000,"end_ms":3000}])
        self.assertTrue(bad["overlap"]); self.assertEqual(bad["clipped_fraction"],1)
    def test_clean_sample_extraction_does_not_duplicate_same_turn(self):
        audio=array("h",range(32000))
        turns=[{"speaker_id":"a","start_ms":0,"end_ms":1000},{"speaker_id":"a","start_ms":500,"end_ms":1500},{"speaker_id":"b","start_ms":1500,"end_ms":2000}]
        self.assertEqual(len(onboarding.clean_samples(audio,turns,"a")),24000)
    def test_job_metadata_is_owner_scoped_and_does_not_return_internal_fields(self):
        with tempfile.TemporaryDirectory() as t, patch.object(onboarding,"JOBS",Path(t)):
            identifier="00000000-0000-4000-8000-000000000001"
            (Path(t)/(identifier+".json")).write_text(json.dumps({"id":identifier,"employee_id":"alice","recording_id":"private","state":"succeeded","operation":"enroll","created_at":1,"error_code":None,"result":{"profile_id":"p"}}))
            with self.assertRaises(ValueError): onboarding.read_job({"id":"bob"},identifier)
            self.assertNotIn("recording_id",onboarding.read_job({"id":"alice"},identifier))
    def test_no_consent_or_non_uuid_never_opens_audio(self):
        with self.assertRaisesRegex(ValueError,"voice_consent_required"): onboarding.submit({"id":"alice"},"anything",False,"enroll")
        with self.assertRaises(ValueError): onboarding.submit({"id":"alice"},"../secret",True,"enroll")
    def test_public_task_includes_title_without_paths_or_notes(self):
        task={"id":"x","state":"queued","stage":"queued","attempts":0,"error_code":None,"created_at":1,"updated_at":1,"metadata":{"title":"Revisión del roadmap","folder_id":"f","notes":"private","sources":{"microphone":{"path":"private"}}}}
        public=public_task(task)
        self.assertEqual(public["title"],"Revisión del roadmap"); self.assertEqual(public["folder_id"],"f")
        self.assertNotIn("notes",public); self.assertNotIn("sources",public)
    def test_encrypted_profiles_delete_only_the_current_owner(self):
        with tempfile.TemporaryDirectory() as t, patch.object(onboarding,"ROOT",Path(t)), patch("synth.speakers.keychain.obtain",return_value=b"k"*32), patch("synth.speakers.sync.unshare"):
            with onboarding.closing(onboarding.vault()) as v:
                vector=[1.0]+[0.0]*255
                profile=v.enroll("alice","test-model",[vector]*3,"consent",["hash"],{"clean_speech_seconds":20,"clipped_fraction":0,"rms_dbfs":-20,"overlap":False})
            with self.assertRaisesRegex(ValueError,"profile_missing"): onboarding.delete({"id":"bob"},profile)
            self.assertTrue(onboarding.delete({"id":"alice"},profile)["deleted"])

if __name__=="__main__": unittest.main()
