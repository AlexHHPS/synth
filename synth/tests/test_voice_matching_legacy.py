import unittest
from unittest.mock import patch
from pathlib import Path
import json
import tempfile
from synth.speakers.matching_legacy import classify, profile_signature, identify
from synth.speakers.profiles import normalized

class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('synth.speakers.matching_legacy.configured_space',return_value='cosine256'))
    def test_known_profile_is_only_a_pilot_candidate(self):
        p=[1.0]+[0.0]*255
        r=classify([p,p,p],[{"id":"p","employee_id":"alice","vector":p}])
        self.assertEqual(r['state'],'candidate'); self.assertIsNone(r['employee_id']); self.assertEqual(r['matching_mode'],'pilot_uncalibrated')
    def test_unknown_and_short_samples_abstain(self):
        p=[1.0]+[0.0]*255; x=[0.0,1.0]+[0.0]*254
        self.assertEqual(classify([x,x,x],[{"id":"p","vector":p}])['state'],'unknown')
        self.assertEqual(classify([],[{"id":"p","vector":p}])['reason'],'short_or_unclean')
    def test_two_indistinguishable_employees_are_ambiguous(self):
        p=[1.0]+[0.0]*255
        r=classify([p,p,p],[{"id":"a","employee_id":"alice","vector":p},{"id":"b","employee_id":"bob","vector":p}]); self.assertEqual(r['state'],'unknown')
    def test_reenrolling_same_person_does_not_create_false_ambiguity(self):
        p=[1.0]+[0.0]*255
        r=classify([p,p,p],[{"id":"a","employee_id":"alice","vector":p},{"id":"b","employee_id":"alice","vector":p}]);self.assertEqual(r['state'],'candidate')
    def test_one_matching_window_is_not_sufficient(self):
        p=[1.0]+[0.0]*255;x=normalized([.6,.8]+[0.0]*254)
        r=classify([p,x,x],[{"id":"p","vector":p}]); self.assertEqual(r['state'],'unknown')
    def test_average_of_two_other_registered_voices_cannot_invent_a_third_identity(self):
        alice=[1.0]+[0.0]*255
        bob=normalized([.7,.71414284285]+[0.0]*254)
        carol=normalized([.7,-.71414284285]+[0.0]*254)
        with patch('synth.speakers.matching_legacy.PILOT_THRESHOLD',.65):
            r=classify([bob,carol],[{'id':'alice','vector':alice},{'id':'bob','vector':bob},{'id':'carol','vector':carol}])
        self.assertEqual(r['state'],'unknown')
        self.assertEqual(r['supporting_windows'],0)
        self.assertGreater(r['similarity'],.99)

    def test_low_pilot_similarity_and_opposite_windows_abstain(self):
        p=[1.0]+[0.0]*255
        weak=normalized([.8,.6]+[0.0]*254)
        self.assertEqual(classify([weak,weak],[{'id':'p','vector':p}])['state'],'unknown')
        self.assertEqual(classify([p,[-value for value in p]],[{'id':'p','vector':p}])['reason'],'inconsistent_voice_windows')
    def test_incompatible_model_abstains_before_opening_audio(self):
        with tempfile.TemporaryDirectory() as t:
            lock=Path(t)/'models.json'; lock.write_text(json.dumps({'model_revision':'current'}))
            with patch('synth.speakers.native.LOCK',lock), patch('synth.speakers.sync.catalog',return_value=([{'id':'p','model':'old'}],'signature','fresh')), patch('synth.speakers.matching_legacy.samples') as audio:
                r=identify({'id':'alice'},{'microphone':{'path':'missing'}},{},Path(t)/'missing')
            self.assertEqual(r['reason'],'no_model_compatible_profiles')
            self.assertEqual(r['active_profiles'],0)
            self.assertEqual(r['matches'],{})
            audio.assert_not_called()
    def test_overlap_abstains_without_comparing_embeddings(self):
        p=[1.0]+[0.0]*255
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); lock=root/'models.json'; lock.write_text(json.dumps({'model_revision':'current'})); wav=root/'capture.wav'; wav.write_bytes(b'fixture')
            profiles=[{'id':'p','model':'current','vector':p}]
            turns=[{'speaker_id':'a','start_ms':0,'end_ms':15000},{'speaker_id':'b','start_ms':5000,'end_ms':20000}]
            with patch('synth.speakers.native.LOCK',lock), patch('synth.speakers.sync.catalog',return_value=(profiles,'signature','fresh')), patch('synth.worker.journal.file_hash',return_value='hash'), patch('synth.speakers.matching_legacy.samples',return_value=[0]*400000), patch('synth.speakers.matching_legacy.embed_chunk') as embed:
                r=identify({'id':'alice'},{'microphone':{'path':str(wav),'sha256':'hash'}},{'microphone':{'segments':turns}},root)
            self.assertEqual({m['reason'] for m in r['matches'].values()},{'overlap'})
            embed.assert_not_called()

    def test_one_failed_speaker_does_not_discard_other_comparisons(self):
        p=[1.0]+[0.0]*255
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); lock=root/'models.json'; lock.write_text(json.dumps({'model_revision':'current'}))
            wav=root/'capture.wav'; wav.write_bytes(b'fixture')
            profiles=[{'id':'p','employee_id':'alice','model':'current','vector':p}]
            turns=[{'speaker_id':'a','start_ms':0,'end_ms':12000}, {'speaker_id':'b','start_ms':13000,'end_ms':25000}]
            with patch('synth.speakers.native.LOCK',lock), \
                 patch('synth.speakers.sync.catalog',return_value=(profiles,'signature','fresh')), \
                 patch('synth.worker.journal.file_hash',return_value='hash'), \
                 patch('synth.speakers.matching_legacy.samples',return_value=[0]*400000), \
                 patch('synth.speakers.matching_legacy.embed_chunk',side_effect=[ValueError('speaker_inference_failed'),ValueError('speaker_inference_failed'),p,p,p,p]):
                r=identify({'id':'alice'},{'microphone':{'path':str(wav),'sha256':'hash'}},{'microphone':{'segments':turns}},root)
            self.assertEqual(r['matches']['microphone:a']['reason'],'voice_embedding_failed')
            self.assertEqual(r['matches']['microphone:a']['cause'],'speaker_inference_failed')
            self.assertEqual(r['matches']['microphone:b']['state'],'candidate')
            self.assertIsNone(r['matches']['microphone:b']['employee_id'])

    def test_two_matching_windows_survive_one_rejected_crop(self):
        p=[1.0]+[0.0]*255
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); lock=root/'models.json';lock.write_text(json.dumps({'model_revision':'current'}))
            wav=root/'capture.wav';wav.write_bytes(b'fixture')
            turns=[{'speaker_id':'a','start_ms':0,'end_ms':20000}]
            with patch('synth.speakers.native.LOCK',lock), \
                 patch('synth.speakers.sync.catalog',return_value=([{'id':'p','employee_id':'alice','model':'current','vector':p}],'signature','fresh')), \
                 patch('synth.worker.journal.file_hash',return_value='hash'), \
                 patch('synth.speakers.matching_legacy.samples',return_value=[0]*400000), \
                 patch('synth.speakers.matching_legacy.embed_chunk',side_effect=[p,ValueError('speaker_inference_failed'),p]):
                r=identify({'id':'alice'},{'microphone':{'path':str(wav),'sha256':'hash'}},{'microphone':{'segments':turns}},root)
            outcome=r['matches']['microphone:a']
            self.assertEqual(outcome['state'],'candidate')
            self.assertEqual(outcome['valid_windows'],2)
            self.assertEqual(outcome['rejected_windows'],1)
            self.assertIsNone(outcome['employee_id'])

    def test_projection_of_existing_profiles_and_new_windows_is_explicit_and_private(self):
        p=[1.0]+[0.0]*255
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);lock=root/'models.json';lock.write_text(json.dumps({'model_revision':'current'}))
            wav=root/'capture.wav';wav.write_bytes(b'fixture')
            turns=[{'speaker_id':'a','start_ms':0,'end_ms':20000}]
            profiles=[{'id':'p','employee_id':'alice','model':'current','vector':p}]
            with patch('synth.speakers.native.LOCK',lock), \
                 patch('synth.speakers.sync.catalog',return_value=(profiles,'signature','fresh')), \
                 patch('synth.worker.journal.file_hash',return_value='hash'), \
                 patch('synth.speakers.matching_legacy.samples',return_value=[0]*320000), \
                 patch('synth.speakers.matching_legacy.embed_chunk',return_value=p), \
                 patch('synth.speakers.native.project_embeddings',side_effect=lambda vectors,*args,**kwargs:[[1.0]+[0.0]*127 for _ in vectors]) as project:
                result=identify({'id':'alice'},{'microphone':{'path':str(wav),'sha256':'hash'}},
                                {'microphone':{'segments':turns}},root,embedding_space='plda_rho128_v1')
            self.assertEqual(result['embedding_space'],'plda_rho128_v1')
            self.assertEqual(project.call_count,2)
            self.assertEqual(result['matches']['microphone:a']['state'],'candidate')
            self.assertIsNone(result['matches']['microphone:a']['employee_id'])
            self.assertEqual(profiles[0]['vector'],p)

    def test_failed_projection_for_one_voice_preserves_other_results(self):
        p=[1.0]+[0.0]*255;projected=[1.0]+[0.0]*127
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);lock=root/'models.json';lock.write_text(json.dumps({'model_revision':'current'}))
            wav=root/'capture.wav';wav.write_bytes(b'fixture')
            turns=[{'speaker_id':'a','start_ms':0,'end_ms':20000},
                   {'speaker_id':'b','start_ms':21000,'end_ms':41000}]
            with patch('synth.speakers.native.LOCK',lock), \
                 patch('synth.speakers.sync.catalog',return_value=([{'id':'p','employee_id':'alice','model':'current','vector':p}],'signature','fresh')), \
                 patch('synth.worker.journal.file_hash',return_value='hash'), \
                 patch('synth.speakers.matching_legacy.samples',return_value=[0]*656000), \
                 patch('synth.speakers.matching_legacy.embed_chunk',return_value=p), \
                 patch('synth.speakers.native.project_embeddings',side_effect=[[projected],ValueError('speaker_projection_failed'),[projected]*3]):
                result=identify({'id':'alice'},{'microphone':{'path':str(wav),'sha256':'hash'}},
                                {'microphone':{'segments':turns}},root,embedding_space='plda_rho128_v1')
            self.assertEqual(result['matches']['microphone:a']['reason'],'voice_projection_failed')
            self.assertEqual(result['matches']['microphone:b']['state'],'candidate')

if __name__=='__main__': unittest.main()
