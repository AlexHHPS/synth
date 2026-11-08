from array import array
import unittest
from synth.speakers.windows import voice_windows, clean_intervals


class VoiceWindowTests(unittest.TestCase):
    def windows(self, duration, turns=None):
        audio = array('h', (i % 32000 for i in range(duration*16000)))
        return voice_windows(audio, turns or [{'speaker_id':'a','start_ms':0,'end_ms':duration*1000}], 'a')

    def assert_disjoint(self, windows):
        spans = sorted(tuple(span) for _, evidence in windows for span in evidence['source_ranges_ms'])
        self.assertTrue(all(left[1] <= right[0] for left, right in zip(spans, spans[1:])))
        self.assertTrue(all(len(chunk) == 80000 for chunk, _ in windows))

    def test_twelve_seconds_produce_two_independent_votes_instead_of_three_overlapping_ones(self):
        windows, report = self.windows(12)
        self.assertEqual(len(windows), 2)
        self.assert_disjoint(windows)
        self.assertEqual(report['window_policy'], 'disjoint_5s_v1')

    def test_long_recording_uses_beginning_middle_and_end_without_shared_audio(self):
        windows, _ = self.windows(30)
        self.assertEqual(len(windows), 3)
        self.assert_disjoint(windows)
        self.assertGreater(windows[-1][1]['source_ranges_ms'][0][0], 24000)

    def test_partial_overlap_removes_only_contaminated_intervals_with_boundary_guard(self):
        turns = [{'speaker_id':'a','start_ms':0,'end_ms':30000},
                 {'speaker_id':'b','start_ms':12000,'end_ms':14000}]
        windows, report = self.windows(30, turns)
        self.assertEqual(len(windows), 3)
        self.assertEqual(report['overlap_seconds'], 2)
        self.assert_disjoint(windows)
        for _, evidence in windows:
            for start, end in evidence['source_ranges_ms']:
                self.assertTrue(end <= 11880 or start >= 14120)

    def test_short_turns_can_form_votes_but_no_source_sample_is_reused(self):
        turns = [{'speaker_id':'a','start_ms':i,'end_ms':i+2500} for i in range(0,30000,5000)]
        windows, _ = self.windows(30, turns)
        self.assertEqual(len(windows), 2)
        self.assert_disjoint(windows)
        self.assertTrue(all(e['joined_turns'] for _,e in windows))

    def test_duplicate_turns_cannot_create_more_evidence_and_full_overlap_stays_unknown(self):
        turns = [{'speaker_id':'a','start_ms':0,'end_ms':7000}]*3
        self.assertEqual(self.windows(10, turns)[0], [])
        turns += [{'speaker_id':'b','start_ms':0,'end_ms':7000}]
        windows, report = self.windows(10, turns)
        self.assertEqual(windows, [])
        self.assertEqual(report['clean_seconds'], 0)

    def test_invalid_or_out_of_audio_turns_never_slice_unowned_samples(self):
        with self.assertRaisesRegex(ValueError, 'voice_turn_invalid'):
            clean_intervals([{'speaker_id':'a','start_ms':-1,'end_ms':2}], 'a', 100)
        self.assertEqual(clean_intervals([{'speaker_id':'a','start_ms':1000,'end_ms':2000}], 'a', 100)[0], [])
