import unittest
from synth.worker.commitment_review import validate_review,review


class CommitmentReviewTests(unittest.TestCase):
    def setUp(self):
        self.candidates={'t0':{'citations':['t1']},'d0':{'citations':['t2']}}
        self.lines={'t1':{'text':'Yo prepararé el inventario mañana.'},'t2':{'text':'Acordamos mantener el archivo.'}}

    def test_exact_quotes_must_belong_to_the_selected_candidates_citations(self):
        self.assertEqual(validate_review({'items':[{'id':'t0','evidence':[{'turn_id':'t1','quote':'prepararé el inventario'}]}]},self.candidates,self.lines),['t0'])
        for item in [
            {'id':'t0','evidence':[{'turn_id':'t2','quote':'mantener el archivo'}]},
            {'id':'d0','evidence':[{'turn_id':'t2','quote':'Acordamos eliminar el archivo.'}]},
            {'id':'invented','evidence':[{'turn_id':'t1','quote':'prepararé el inventario'}]},
            {'id':'t0','evidence':[]},
            {'id':'t0','evidence':[{'turn_id':'t1','quote':'prepararé el inventario'}],'owner':'invented'},
        ]:
            with self.subTest(item=item),self.assertRaises(ValueError):validate_review({'items':[item]},self.candidates,self.lines)

    def test_duplicate_or_malformed_selection_cannot_pass(self):
        item={'id':'t0','evidence':[{'turn_id':'t1','quote':'prepararé el inventario'}]}
        for data in ({'items':[item,item]},{'items':'t0'},{'items':[],'tasks':[]},{'items':[{'id':[],'evidence':[]}]}):
            with self.subTest(data=data),self.assertRaises(ValueError):validate_review(data,self.candidates,self.lines)

    def test_review_keeps_original_fields_and_anonymous_self_commitment(self):
        task={'title':'Preparar el inventario','owner':'sin asignar','due_date':None,'citations':['t1']}
        decision={'decision':'Mantener el archivo','status':'pendiente de validar','citations':['t2']}
        lines=[{'id':key,'text':value['text'],'line':key+': '+value['text']} for key,value in self.lines.items()]
        def ask(complete,system,user,validate,responses,tokens):
            return validate({'items':[{'id':'t0','evidence':[{'turn_id':'t1','quote':'prepararé el inventario mañana'}]}]})
        tasks,decisions,audit=review(None,[task],[decision],lines,'',ask,'',[])
        self.assertIs(tasks[0],task)
        self.assertEqual(decisions,[])
        self.assertEqual(audit['candidates'],2)
