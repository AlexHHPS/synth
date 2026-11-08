import copy
import unittest
from uuid import uuid4

from synth.speakers.finalize_capture import map_confirmed
from synth.contracts.transcript import Transcript


class FinalizeVoiceCaptureTests(unittest.TestCase):
    def setUp(self):
        self.content = {'language':'es','duration_ms':20000,'model_fingerprint':'qwen:test','segments':[
            {'id':'s0','text':'No está aprobado','start_ms':0,'end_ms':3000,'source_id':'system','speaker_id':'system:S1'},
            {'id':'s1','text':'Lo revisaremos','start_ms':10000,'end_ms':13000,'source_id':'system','speaker_id':'system:S1'}],
            'turns':[{'id':'t000000','text':'No está aprobado','start_ms':0,'end_ms':3000,'speaker_id':'system:S1','segment_ids':['s0'],'word_count':3}]}
        self.status = {'capture_id':str(uuid4()),'matches':[{'confirmed':True,'source':'system','speech_spans_ms':[[0,3000]],
            'candidate_employee_id':str(uuid4()),'candidate_name':'Persona confirmada'}]}

    def test_only_confirmed_spans_receive_anonymous_labels_until_the_separate_owner_action(self):
        before = copy.deepcopy(self.content)
        content, actions = map_confirmed(self.content,self.status)
        self.assertEqual(self.content,before)
        self.assertTrue(content['segments'][0]['speaker_id'].startswith('live-confirmed:'))
        self.assertEqual(content['segments'][1]['speaker_id'],'system:S1')
        self.assertEqual(content['turns'][0]['speaker_id'],content['segments'][0]['speaker_id'])
        self.assertTrue(all(s.get('employee_id') is None for s in content['segments']))
        self.assertNotIn('speakers',content)
        Transcript.model_validate(content)
        self.assertEqual(actions[0]['name'],'Persona confirmada')
        self.assertEqual([s['text'] for s in content['segments']],['No está aprobado','Lo revisaremos'])

    def test_suggestions_overlap_and_partial_coverage_cannot_publish_a_name(self):
        for change in ({'confirmed':False},{'speech_spans_ms':[[0,1000]]}):
            status = {**self.status,'matches':[{**self.status['matches'][0],**change}]}
            _,actions = map_confirmed(self.content,status)
            self.assertEqual(actions,[])
        status = {**self.status,'matches':[self.status['matches'][0],{**self.status['matches'][0],'confirmed':False}]}
        _,actions = map_confirmed(self.content,status)
        self.assertEqual(actions,[])

    def test_two_adjacent_windows_of_the_same_confirmed_person_keep_identity_and_citations(self):
        first = self.status['matches'][0]
        self.status['matches'] = [{**first,'speech_spans_ms':[[0,1500]]}, {**first,'speech_spans_ms':[[1500,3000]]}]
        result,actions = map_confirmed(self.content,self.status)
        self.assertEqual(len(actions),1)
        self.assertEqual(actions[0]['employee_id'],first['candidate_employee_id'])
        self.assertTrue(result['segments'][0]['speaker_id'].startswith('live-confirmed:'))
        self.assertEqual(result['segments'][1],self.content['segments'][1])
        self.assertEqual(result['turns'][0]['segment_ids'],['s0'])
        self.assertEqual(result['turns'][0]['text'],self.content['turns'][0]['text'])
        self.assertIsNone(result['segments'][0]['employee_id'])

    def test_adjacent_windows_cannot_bridge_different_people_missing_audio_or_suggestions(self):
        first = self.status['matches'][0]
        for change in ({'candidate_employee_id':str(uuid4())}, {'confirmed':False},
                       {'candidate_employee_id':None}, {'speech_spans_ms':[[2000,3000]]},
                       {'speech_spans_ms':[[1000,3000]]}):
            with self.subTest(change=change):
                status={**self.status,'matches':[{**first,'speech_spans_ms':[[0,1500]]},
                                                {**first,'speech_spans_ms':[[1500,3000]],**change}]}
                result,actions=map_confirmed(self.content,status)
                self.assertEqual(actions,[])
                self.assertEqual(result,self.content)
