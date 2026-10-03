import json
from pathlib import Path
import tempfile
import unittest
from synth.worker.transcript_quality import repetition_report, select_canonical, live_transcript


class TranscriptQualityTests(unittest.TestCase):
    def batch(self, count=30):
        return {'language': 'es', 'duration_ms': 90000, 'model_fingerprint': 'whisper:test',
                'segments': [{'id': str(i), 'text': 'La misma frase', 'start_ms': i*1000,
                              'end_ms': i*1000+500, 'source_id': 'microphone'} for i in range(count)]}

    def closed(self, root, receipt_id='capture'):
        source = root/'meeting/sources';source.mkdir(parents=True)
        (source/'capture.json').write_text(json.dumps({'state':'closed','capture_id':receipt_id}))
        (source.parent/'transcripts.json').write_text(json.dumps({'segments':[
            {'text':'Decidimos preparar el piloto','audio_start_time':2.0,'audio_end_time':4.0},
            {'text':'Pendiente de revisión','audio_start_time':5.0,'audio_end_time':7.0}]}))
        return {'metadata':{'capture_id':'capture','sources':{'microphone':{'path':str(source/'microphone.wav')}}}}

    def test_short_legitimate_repetition_is_not_flagged(self):
        self.assertFalse(repetition_report({'mic':self.batch(2)})['mic']['suspicious'])

    def test_diverse_batch_remains_unchanged(self):
        with tempfile.TemporaryDirectory() as t:
            batch=self.batch()
            for i,s in enumerate(batch['segments']):s['text']=f'Contenido número {i}'
            chosen,report=select_canonical({'microphone':batch},batch,{},Path(t))
            self.assertIs(chosen,batch);self.assertEqual(report['selected'],'batch')

    def test_repetitive_batch_uses_exact_live_words_without_inventing_speakers(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);task=self.closed(root);batch=self.batch()
            chosen,report=select_canonical({'microphone':batch},batch,task,root)
            self.assertEqual([s['text'] for s in chosen['segments']],['Decidimos preparar el piloto','Pendiente de revisión'])
            self.assertTrue(all(s['source_id']=='live_mix' and s['speaker_id'] is None and s['employee_id'] is None for s in chosen['segments']))
            self.assertEqual(report['selected'],'capture_live')
            self.assertEqual(chosen['segments'][0]['start_ms'],2000)

    def test_missing_live_and_wrong_capture_id_block_publication(self):
        for mismatch in (False,True):
            with tempfile.TemporaryDirectory() as t:
                root=Path(t);task=self.closed(root,receipt_id='other' if mismatch else 'capture')
                if not mismatch:(root/'meeting/transcripts.json').unlink()
                batch=self.batch()
                with self.assertRaisesRegex(ValueError,'batch_transcription_repetitive'):
                    select_canonical({'microphone':batch},batch,task,root)

    def test_symlink_cannot_import_another_meetings_live_text(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);task=self.closed(root);live=root/'meeting/transcripts.json'
            other=root/'other.json';other.write_bytes(live.read_bytes());live.unlink();live.symlink_to(other)
            batch=self.batch()
            with self.assertRaisesRegex(ValueError,'batch_transcription_repetitive'):
                select_canonical({'microphone':batch},batch,task,root)

    def test_invalid_timestamps_are_not_clamped(self):
        for start,end in [(float('nan'),1),(-1,1),(3,1),(1,100)]:
            with self.assertRaises(ValueError):
                live_transcript({'segments':[{'text':'Texto','audio_start_time':start,'audio_end_time':end}]},90000,'a'*64)

    def test_channel_already_excluded_by_diarization_does_not_force_fallback(self):
        with tempfile.TemporaryDirectory() as t:
            discarded=self.batch()
            canonical=self.batch(1)
            canonical['segments'][0].update(text='Conversación válida',source_id='system')
            chosen,report=select_canonical({'microphone':discarded},canonical,{},Path(t))
            self.assertIs(chosen,canonical)
            self.assertEqual(report['selected'],'batch')
