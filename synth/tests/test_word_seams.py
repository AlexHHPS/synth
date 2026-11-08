import unittest
from synth.worker.word_seams import resolve_cut


def word(text, start, end):
    return {'text': text, 'start_ms': start, 'end_ms': end}


class WordSeamTests(unittest.TestCase):
    def test_shared_ownership_repairs_both_missing_and_duplicate_words(self):
        left=word('No.',11920,12080);right=word('No.',11880,12040)
        for a,b in [(left,right),(right,left)]:
            merged,reviews=resolve_cut([a],[b],12000)
            self.assertEqual([w['text'] for w in merged],['No.'])
            self.assertEqual(reviews,[])

    def test_shared_ownership_preserves_interior_hypotheses_without_adding_context(self):
        old=word('Lateral',14000,14500);new=word('Interior',14010,14510)
        words,reviews=resolve_cut([old],[new],12000)
        self.assertEqual(words,[new])
        self.assertEqual(reviews,[])

    def test_unmatched_negation_is_review_data_and_does_not_turn_into_an_agreement(self):
        words,reviews=resolve_cut([word('No.',12000,12100)],[word('Sí.',11900,12000)],12000)
        self.assertEqual(words,[])
        self.assertEqual([r['text'] for r in reviews],['No.','Sí.'])

    def test_repeated_real_no_words_are_not_textually_deduplicated(self):
        left=[word('No,',11600,11700),word('no.',11900,12100)]
        right=[word('No,',11620,11720),word('no.',11920,12120)]
        merged,reviews=resolve_cut(left,right,12000)
        self.assertEqual([w['text'] for w in merged],['No,','no.'])
        self.assertEqual(reviews,[])

    def test_missing_negation_is_kept_but_not_treated_as_agreed_evidence(self):
        words,reviews=resolve_cut([word('No.',11900,12000)],[],12000)
        self.assertEqual(words[0]['text'],'No.')
        self.assertEqual(words[0]['evidence_state'],'needs_review')
        self.assertEqual(reviews[0]['text'],'No.')

    def test_conflicting_default_word_is_flagged_and_other_hypothesis_is_retained(self):
        words,reviews=resolve_cut([word('No.',11900,12000)],[word('Sí.',12000,12100)],12000)
        self.assertTrue(all(w['evidence_state']=='needs_review' for w in words))
        self.assertEqual({r['text'] for r in reviews},{'No.','Sí.'})

    def test_invalid_timestamps_and_boundaries_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'seam_word_invalid'):
            resolve_cut([word('No.',-1,1)],[],12000)
        with self.assertRaisesRegex(ValueError,'seam_boundary_invalid'):
            resolve_cut([],[],-1)
