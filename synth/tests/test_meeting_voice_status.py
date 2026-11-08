import json
from pathlib import Path
import tempfile
import unittest
from synth.speakers.meeting_status import meeting_status, audio_available, transcript_hash
from synth.speakers.windows import analysis_policy


class MeetingVoiceStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.content = {'segments': [{'speaker_id': 'microphone:S1', 'text': 'Synthetic speech'}]}
        self.transcript = {'version': 2, 'content': self.content}
        self.task = {'id': 'task', 'state': 'succeeded', 'checkpoints': {'normalized': {'microphone': {'path': str(self.root/'gone.wav')}}}}
        (self.root/'transcript.json').write_text(json.dumps(self.content))
        self.result = {'state': 'pilot_uncalibrated', 'profile_signature': 'catalog',
                       'analysis_policy': analysis_policy(),
                       'transcript_hash': transcript_hash(self.content),
                       'matches': {'microphone:S1': {'state': 'candidate', 'label': 'Synthetic person'},
                                   'system:unused': {'state': 'candidate', 'label': 'Other person'}}}
        self.save()

    def save(self):
        (self.root/'identity-status.json').write_text(json.dumps(self.result))

    def test_automatic_result_survives_audio_deletion_and_excludes_unused_speakers(self):
        result = meeting_status(self.task, self.transcript, self.root, 'catalog')
        self.assertEqual(result['processing_mode'], 'automatic')
        self.assertEqual(result['transcript_version'], 2)
        self.assertEqual(set(result['matches']), {'microphone:S1'})
        self.assertFalse(audio_available(self.task))

    def test_changed_catalog_hides_names_without_attempting_deleted_audio(self):
        result = meeting_status(self.task, self.transcript, self.root, 'revoked-profile')
        self.assertEqual(result['reason'], 'profile_changed')
        self.assertEqual(result['matches'], {})

    def test_changed_catalog_is_reevaluated_from_stored_prints(self):
        fresh = {'state': 'suggestions', 'matches': {'microphone:S1': {'state': 'ask', 'candidate_name': 'Ana'}}}
        result = meeting_status(self.task, self.transcript, self.root, 'revoked-profile', reidentify=lambda: fresh)
        self.assertEqual(result['matches']['microphone:S1']['state'], 'ask')

    def test_resolved_speakers_get_no_more_suggestions(self):
        named = {'version': 3, 'content': {**self.content, 'speakers': {'microphone:S1': {'status': 'confirmed', 'name': 'Ana'}}}}
        result = meeting_status(self.task, named, self.root, 'catalog')
        self.assertEqual(result['matches'], {})
        self.assertEqual(result['resolved'], ['microphone:S1'])

    def test_recovered_text_cannot_inherit_old_speaker_assignments(self):
        changed = {'version': 3, 'content': {'segments': [{'speaker_id': None, 'text': 'Recovered text'}]}}
        result = meeting_status(self.task, changed, self.root, 'catalog')
        self.assertEqual(result['reason'], 'transcript_version_changed')
        self.assertEqual(result['matches'], {})

    def test_empty_audio_descriptors_are_not_available(self):
        self.task['checkpoints']['normalized'] = {}
        self.assertFalse(audio_available(self.task))

    def test_hash_mismatch_never_returns_cached_names(self):
        self.result['transcript_hash'] = 'different'
        self.save()
        self.assertEqual(meeting_status(self.task, self.transcript, self.root, 'catalog')['matches'], {})

    def test_old_window_policy_cannot_restore_candidate_names(self):
        self.result.pop('analysis_policy')
        self.save()
        result = meeting_status(self.task, self.transcript, self.root, 'catalog')
        self.assertEqual(result['reason'], 'analysis_policy_changed')
        self.assertEqual(result['matches'], {})
