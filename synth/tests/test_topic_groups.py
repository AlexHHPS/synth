import unittest
from synth.worker.topic_groups import validate_groups
from synth.worker.minutes import validate_topic,generate_v2
from synth.worker.document_context import MeetingContext


class TopicGroupTests(unittest.TestCase):
    def setUp(self):
        self.topics=[{'title':str(i),'ids':['t'+str(i)]} for i in range(6)]
        self.lines={'t'+str(i):{'id':'t'+str(i),'line':'Synthetic source','text':'Synthetic source'} for i in range(6)}

    def test_global_groups_preserve_all_sources_once_in_source_order(self):
        result=validate_groups({'groups':[{'title':'Same subject','indices':[4,0,2]},{'title':'Other','indices':[1,3,5]}]},self.topics,self.lines)
        self.assertEqual(result[0]['ids'],['t0','t2','t4'])
        self.assertEqual(sorted(i for t in result for i in t['ids']),list(self.lines))

    def test_missing_duplicate_invented_boolean_and_oversized_groups_fail(self):
        for indices in ([0,1,2],[0,1,2,3,4,5,0],[0,1,2,3,4,6],[False,1,2,3,4,5]):
            with self.subTest(indices=indices),self.assertRaises(ValueError):
                validate_groups({'groups':[{'title':'Topic','indices':indices}]},self.topics,self.lines)
        self.lines['t0']['line']='x'*32001
        with self.assertRaises(ValueError):validate_groups({'groups':[{'title':'Topic','indices':list(range(6))}]},self.topics,self.lines)

    def test_missing_decision_status_cannot_silently_become_an_agreement(self):
        check=validate_topic({'title':'Synthetic','lines':[{'id':'t0','text':'Podríamos probar otra opción.','speaker':'Voz'}]},set(),set())
        with self.assertRaisesRegex(ValueError,'estado_decision_invalido'):
            check({'decisions':[{'decision':'Probar otra opción','citations':['t0']}]})

    def test_production_cannot_resume_an_experimental_outline_checkpoint(self):
        with self.assertRaisesRegex(ValueError,'experimental_minutes_checkpoint'):
            generate_v2({'segments':[]},MeetingContext(),lambda *_:self.fail('No model call expected'),
                        {'outline':{'policy':'global_outline_complete_partition_v1'}})
