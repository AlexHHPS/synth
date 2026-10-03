import unittest
from synth.speakers.evaluation import evaluate


class EvaluationTests(unittest.TestCase):
    def dataset(self):
        axes = [[float(i == j) for i in range(256)] for j in range(4)]
        return {'kind': 'synthetic',
                'profiles': [{'id': str(i), 'employee_id': str(i), 'vector': axes[i]} for i in range(3)],
                'trials': [{'id': str(i), 'device': 'fixture-device', 'condition': 'orthogonal',
                            'independent_of_enrollment': True, 'expected_employee_id': str(i) if i < 3 else None,
                            'vectors': [axes[i]]*3} for i in range(4)]}

    def test_three_enrolled_and_unknown_metrics_do_not_certify_identity(self):
        r = evaluate(self.dataset())
        self.assertEqual(r['counts']['correct_candidates'], 3)
        self.assertEqual(r['counts']['abstentions'], 1)
        self.assertEqual(r['coverage'], .75)
        self.assertFalse(r['confirms_identity'])

    def test_wrong_candidate_counts_false_positive_and_false_negative(self):
        d = self.dataset(); d['trials'][0]['expected_employee_id'] = '1'
        r = evaluate(d)
        self.assertEqual(r['counts']['false_positive'], 1)
        self.assertEqual(r['counts']['false_negative'], 1)

    def test_short_samples_abstain(self):
        d = self.dataset(); d['trials'][0]['vectors'] = []
        self.assertEqual(evaluate(d)['counts']['false_negative'], 1)

    def test_invalid_and_non_independent_data_rejected(self):
        for change in ('dimensions', 'nan', 'independence'):
            d = self.dataset()
            if change == 'dimensions': d['profiles'][0]['vector'] = [1]
            elif change == 'nan': d['profiles'][0]['vector'][0] = float('nan')
            else: d['trials'][0]['independent_of_enrollment'] = False
            with self.assertRaises(ValueError): evaluate(d)
