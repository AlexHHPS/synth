import tempfile
from pathlib import Path
import subprocess
import sys
import unittest

from synth.worker.desktop_pipeline import canonical_transcript, inference_lock, save_json, restore_json


class PipelineTests(unittest.TestCase):
    def test_one_voice_with_empty_system_audio_still_produces_transcript(self):
        mic = {"language": "es", "model_fingerprint": "whisper:test", "duration_ms": 2000,
            "segments": [{"id": "s0", "start_ms": 0, "end_ms": 1000, "text": "Mi nota personal", "source_id": "microphone"}]}
        system = {**mic, "duration_ms": 2100, "segments": [{**mic["segments"][0], "text": "Artefacto de ASR", "source_id": "system"}]}
        turns = {"segments": [{"start_ms": 0, "end_ms": 1000, "speaker_id": "speaker_0"}], "speech_state": "detected"}
        result = canonical_transcript({"microphone": mic, "system": system},
            {"microphone": turns, "system": {"speech_state": "no_usable_speech", "segments": []}})
        self.assertEqual(result["duration_ms"], 2100)
        self.assertEqual([s["text"] for s in result["segments"]], ["Mi nota personal"])
        self.assertEqual(result["segments"][0]["speaker_id"], "microphone:local")  # one dominant voice = the local user
        self.assertIsNone(result["segments"][0]["employee_id"])
        self.assertEqual(system["segments"][0]["text"], "Artefacto de ASR")

    def test_short_note_without_usable_embedding_preserves_words_and_unknown_speaker(self):
        transcript = {"language": "es", "model_fingerprint": "whisper:test", "duration_ms": 1000,
            "segments": [{"id": "s0", "start_ms": 0, "end_ms": 1000, "text": "Idea breve", "source_id": "microphone"}]}
        result = canonical_transcript({"microphone": transcript},
            {"microphone": {"speech_state": "no_usable_speech", "segments": []}})
        self.assertEqual(result["segments"][0]["text"], "Idea breve")
        self.assertIsNone(result["segments"][0]["speaker_id"])

    def test_source_provenance_and_anonymous_labels_survive_merge(self):
        source = {"language": "es", "model_fingerprint": "whisper:test", "duration_ms": 2000,
            "segments": [{"id": "s0", "start_ms": 0, "end_ms": 1000, "text": "Texto", "source_id": "import"}]}
        turns = {"segments": [{"start_ms": 0, "end_ms": 1000, "speaker_id": "speaker_0"}]}
        result = canonical_transcript({"system": source, "microphone": source},
                                      {"system": turns, "microphone": turns})
        self.assertEqual([s["id"] for s in result["segments"]], ["s000000", "s000001"])
        # Remote meeting (the system channel has speech): the microphone is the local user.
        self.assertEqual({s["speaker_id"] for s in result["segments"]},
                         {"system:speaker_0", "microphone:local"})
        self.assertTrue(all(s["employee_id"] is None for s in result["segments"]))

    def test_corrupt_checkpoint_cannot_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = save_json(root / "content.json", {"value": 1})
            self.assertEqual(restore_json(receipt, root), {"value": 1})
            (root / "content.json").write_text('{"value":2}')
            with self.assertRaisesRegex(ValueError, "checkpoint_file_invalid"):
                restore_json(receipt, root)

    def test_aligned_words_resolve_a_phrase_crossing_two_speakers(self):
        source = {'language': 'es', 'model_fingerprint': 'qwen:test', 'duration_ms': 2000,
                  'segments': [{'id': 's0', 'text': 'Hola. De acuerdo.', 'start_ms': 0, 'end_ms': 2000}],
                  'words': [{'text': 'Hola.', 'start_ms': 0, 'end_ms': 900},
                            {'text': 'De', 'start_ms': 1100, 'end_ms': 1400},
                            {'text': 'acuerdo.', 'start_ms': 1500, 'end_ms': 2000}]}
        turns = {'segments': [{'speaker_id': 'a', 'start_ms': 0, 'end_ms': 1000},
                              {'speaker_id': 'b', 'start_ms': 1000, 'end_ms': 2000}]}
        result = canonical_transcript({'microphone': source}, {'microphone': turns})
        self.assertEqual([s['text'] for s in result['segments']], ['Hola.', 'De acuerdo.'])
        self.assertEqual([s['speaker_id'] for s in result['segments']], ['microphone:a', 'microphone:b'])
        self.assertTrue(all(s['employee_id'] is None for s in result['segments']))

    def test_word_in_overlapping_voices_stays_unknown(self):
        source = {'language': 'es', 'model_fingerprint': 'qwen:test', 'duration_ms': 2000,
                  'segments': [{'id': 's0', 'text': 'Texto mezclado', 'start_ms': 0, 'end_ms': 2000}],
                  'words': [{'text': 'Texto', 'start_ms': 0, 'end_ms': 800},
                            {'text': 'mezclado', 'start_ms': 1000, 'end_ms': 1800}]}
        turns = {'segments': [{'speaker_id': 'a', 'start_ms': 0, 'end_ms': 2000},
                              {'speaker_id': 'b', 'start_ms': 900, 'end_ms': 2000}]}
        result = canonical_transcript({'microphone': source}, {'microphone': turns})
        self.assertEqual(result['segments'][0]['speaker_id'], 'microphone:a')
        self.assertIsNone(result['segments'][1]['speaker_id'])
        source['words'][1]['text'] = 'alterado'
        with self.assertRaisesRegex(ValueError, 'speaker_word_alignment_mismatch'):
            canonical_transcript({'microphone': source}, {'microphone': turns})

    def test_split_phrase_keeps_asr_uncertainty_and_cites_the_actual_fragments(self):
        block = '11111111-1111-4111-8111-111111111111:system:0'
        source = {'language': 'es', 'model_fingerprint': 'qwen:test', 'duration_ms': 2000,
                  'segments': [{'id': 's000000', 'text': 'Hola. No.', 'start_ms': 0, 'end_ms': 2000,
                                'asr_block_id': block, 'evidence_state': 'needs_review'}]}
        words = [{'text': 'Hola.', 'start_ms': 0, 'end_ms': 900, 'segment_index': 0},
                 {'text': 'No.', 'start_ms': 1100, 'end_ms': 2000, 'segment_index': 0}]
        diarization = {'segments': [{'speaker_id': 'a', 'start_ms': 0, 'end_ms': 1000},
                                    {'speaker_id': 'b', 'start_ms': 1000, 'end_ms': 2000}]}
        evidence = [{'block_id': block, 'source_id': 'system', 'start_ms': 0, 'end_ms': 2000,
                     'raw_text': 'Hola. No.', 'input_sha256': '0'*64, 'model_fingerprint': 'qwen:test', 'reviews': []}]
        result = canonical_transcript({'system': source}, {'system': diarization}, {'system': words}, evidence)
        self.assertEqual([s['text'] for s in result['segments']], ['Hola.', 'No.'])
        self.assertTrue(all(s['asr_block_id'] == block and s['evidence_state'] == 'needs_review' for s in result['segments']))
        self.assertEqual([t['segment_ids'] for t in result['turns']], [['s000000'], ['s000001']])
        self.assertEqual(result['asr_evidence'], evidence)

    def test_new_capture_does_not_infer_the_recorder_from_a_shared_microphone(self):
        block = '11111111-1111-4111-8111-111111111111:microphone:0'
        source = {'language': 'es', 'model_fingerprint': 'qwen:test', 'duration_ms': 5000,
                  'segments': [{'id': 's000000', 'text': 'Mi propuesta.', 'start_ms': 0, 'end_ms': 1000,
                                'source_id': 'microphone', 'asr_block_id': block, 'evidence_state': 'aligned'},
                               {'id': 's000001', 'text': 'No.', 'start_ms': 2000, 'end_ms': 3000,
                                'source_id': 'microphone', 'asr_block_id': block, 'evidence_state': 'aligned'}]}
        remote = {'language': 'es', 'model_fingerprint': 'qwen:test', 'duration_ms': 5000,
                  'segments': [{'id': 's000000', 'text': 'Os escucho.', 'start_ms': 3500, 'end_ms': 4000,
                                'source_id': 'system'}]}
        diarizations = {'microphone': {'segments': [{'speaker_id': 'a', 'start_ms': 0, 'end_ms': 1000},
                                                  {'speaker_id': 'b', 'start_ms': 2000, 'end_ms': 3000}]},
                        'system': {'segments': [{'speaker_id': 'remote', 'start_ms': 3500, 'end_ms': 4000}]}}
        words = {'microphone': [{'text': 'Mi', 'start_ms': 0, 'end_ms': 400, 'segment_index': 0},
                                {'text': 'propuesta.', 'start_ms': 400, 'end_ms': 1000, 'segment_index': 0},
                                {'text': 'No.', 'start_ms': 2000, 'end_ms': 3000, 'segment_index': 1}]}
        evidence = [{'block_id': block, 'source_id': 'microphone', 'start_ms': 0, 'end_ms': 5000,
                     'raw_text': 'Mi propuesta. No.', 'input_sha256': '0'*64, 'model_fingerprint': 'qwen:test', 'reviews': []}]
        result = canonical_transcript({'microphone': source, 'system': remote}, diarizations, words, evidence)
        self.assertEqual([s['speaker_id'] for s in result['segments']], ['microphone:a', 'microphone:b', 'system:remote'])
        self.assertEqual([t['speaker_id'] for t in result['turns']], ['microphone:a', 'microphone:b'])
        self.assertNotIn('microphone:local', str(result))
        self.assertTrue(all(s['employee_id'] is None for s in result['segments']))

    def test_overlapping_voices_remain_unknown_in_new_segments_and_turns(self):
        source = {'language': 'es', 'model_fingerprint': 'qwen:test', 'duration_ms': 2000,
                  'segments': [{'id': 's000000', 'text': 'No.', 'start_ms': 0, 'end_ms': 1000}]}
        turns = {'segments': [{'speaker_id': 'a', 'start_ms': 0, 'end_ms': 1000},
                              {'speaker_id': 'b', 'start_ms': 0, 'end_ms': 1000}]}
        result = canonical_transcript({'system': source}, {'system': turns},
                                      {'system': [{'text': 'No.', 'start_ms': 0, 'end_ms': 1000}]}, [])
        self.assertIsNone(result['segments'][0]['speaker_id'])
        self.assertIsNone(result['turns'][0]['speaker_id'])

    def test_batch_recovery_keeps_microphone_speakers_without_completed_live_receipt(self):
        source = {'language': 'es', 'model_fingerprint': 'qwen:test', 'duration_ms': 3000,
                  'segments': [{'id': 's0', 'text': 'Hola.', 'start_ms': 0, 'end_ms': 1000},
                               {'id': 's1', 'text': 'No.', 'start_ms': 2000, 'end_ms': 3000}]}
        remote = {**source, 'segments': [{'id': 's0', 'text': 'Vale.', 'start_ms': 1000, 'end_ms': 1500}]}
        diarizations = {'microphone': {'segments': [{'speaker_id': 'a', 'start_ms': 0, 'end_ms': 1000},
                                                   {'speaker_id': 'b', 'start_ms': 2000, 'end_ms': 3000}]},
                        'system': {'segments': [{'speaker_id': 'c', 'start_ms': 1000, 'end_ms': 1500}]}}
        words = {'microphone': [{'text': 'Hola.', 'start_ms': 0, 'end_ms': 1000},
                                 {'text': 'No.', 'start_ms': 2000, 'end_ms': 3000}]}
        result = canonical_transcript({'microphone': source, 'system': remote}, diarizations, words,
                                      independent_channels=True)
        self.assertEqual([s['speaker_id'] for s in result['segments']], ['microphone:a', 'system:c', 'microphone:b'])
        self.assertEqual([t['speaker_id'] for t in result['turns']], ['microphone:a', 'microphone:b'])
        self.assertTrue(all(s['employee_id'] is None for s in result['segments']))

    def test_batch_recovery_does_not_assign_overlapping_words_to_nearest_speaker(self):
        source = {'language': 'es', 'model_fingerprint': 'qwen:test', 'duration_ms': 1000,
                  'segments': [{'id': 's0', 'text': 'No.', 'start_ms': 0, 'end_ms': 1000}]}
        diarization = {'segments': [{'speaker_id': 'a', 'start_ms': 0, 'end_ms': 1000},
                                    {'speaker_id': 'b', 'start_ms': 0, 'end_ms': 1000}]}
        result = canonical_transcript({'microphone': source}, {'microphone': diarization},
                                      {'microphone': [{'text': 'No.', 'start_ms': 0, 'end_ms': 1000}]},
                                      independent_channels=True)
        self.assertIsNone(result['segments'][0]['speaker_id'])
        self.assertIsNone(result['turns'][0]['speaker_id'])

    def test_checkpoint_cannot_escape_task_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "task"
            child.mkdir()
            receipt = save_json(root / "other.json", {"value": 1})
            with self.assertRaisesRegex(ValueError, "checkpoint_file_invalid"):
                restore_json(receipt, child)

    def test_os_lock_blocks_second_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            with inference_lock(directory):
                with self.assertRaisesRegex(ValueError, "acoustic_worker_already_running"):
                    with inference_lock(directory):
                        self.fail("second worker started")
            with inference_lock(directory):
                pass

    def test_inference_descendant_keeps_lock_when_parent_releases_its_fd(self):
        # Launch directly, as the packaged acoustic runtime does. A surviving child
        # must prevent a supervisor from starting overlapping acoustic work.
        with tempfile.TemporaryDirectory() as directory:
            process = None
            try:
                with inference_lock(directory) as fd:
                    process = subprocess.Popen([sys.executable, "-c",
                        "import sys; print('ready',flush=True); sys.stdin.readline()"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, pass_fds=(fd,))
                    self.assertEqual(process.stdout.readline().strip(), "ready")
                with self.assertRaisesRegex(ValueError, "acoustic_worker_already_running"):
                    with inference_lock(directory):
                        self.fail("descendant lost lock")
                process.communicate("done\n", timeout=5)
                with inference_lock(directory):
                    pass
            finally:
                if process and process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
