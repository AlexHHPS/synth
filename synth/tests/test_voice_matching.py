import unittest
from synth.speakers.matching import classify, profile_signature
from synth.speakers.profiles import normalized

class MatchingTests(unittest.TestCase):
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

if __name__=='__main__': unittest.main()
