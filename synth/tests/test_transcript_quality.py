import json
from pathlib import Path
import tempfile
import unittest
from synth.worker.transcript_quality import repetition_report, select_canonical, live_transcript, cross_source_report


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

    def test_three_minute_loop_inside_long_meeting_is_flagged(self):
        batch = self.batch(561)
        for i, segment in enumerate(batch['segments']):
            segment.update(text=f'Contenido diferente {i}', start_ms=i*2000, end_ms=(i+1)*2000)
        for segment in batch['segments'][200:321]:
            segment['text'] = 'Y está disponible para los agentes y más fáciles de actualizar.'
        report = repetition_report({'system': batch})['system']
        self.assertLess(report['dominant_phrase_fraction'], .8)
        self.assertTrue(report['suspicious'])
        self.assertEqual(report['repetition_runs'][0]['segments'], 121)

    def test_repeated_short_replies_and_spaced_refrains_are_kept(self):
        batch = self.batch(50)
        for segment in batch['segments']:
            segment['text'] = 'Gracias'
        for segment in batch['segments'][10:]:
            segment['text'] = f'Otro contenido {segment["id"]}'
        self.assertFalse(repetition_report({'mic': batch})['mic']['suspicious'])
        for i, segment in enumerate(batch['segments'][:10]):
            segment.update(text='Vamos a revisar este tema otra vez', start_ms=i*60000, end_ms=i*60000+2000)
        self.assertFalse(repetition_report({'mic': batch})['mic']['suspicious'])

    def duplicated(self, count=6, lag=100):
        segments = []
        for i in range(count):
            for source, delay in [('system', 0), ('microphone', lag)]:
                segments.append({'id': f'{source}{i}', 'text': f'Revisamos la propuesta completa con todo el equipo número {i}',
                                 'start_ms': i*5000+delay, 'end_ms': i*5000+4500+delay, 'source_id': source})
        return {'language': 'es', 'duration_ms': 90000, 'model_fingerprint': 'whisper:test', 'segments': segments}

    def test_sustained_cross_source_duplication_is_flagged_without_mutating_text(self):
        canonical = self.duplicated()
        original = json.dumps(canonical)
        report = cross_source_report(canonical)
        self.assertTrue(report['suspicious'])
        self.assertEqual(report['matching_segments'], 6)
        self.assertEqual(json.dumps(canonical), original)

    def test_later_replies_short_agreements_and_different_speech_are_kept(self):
        self.assertFalse(cross_source_report(self.duplicated(lag=5000))['suspicious'])
        for text in ['Sí, de acuerdo', 'Una respuesta distinta con suficientes palabras para no coincidir en absoluto']:
            canonical = self.duplicated()
            for segment in canonical['segments']:
                if segment['source_id'] == 'microphone': segment['text'] = text
            self.assertFalse(cross_source_report(canonical)['suspicious'])
        self.assertFalse(cross_source_report(self.duplicated(count=1))['suspicious'])

    def test_overlapping_duplicate_segments_cannot_inflate_covered_time(self):
        canonical = self.duplicated()
        for segment in canonical['segments']:
            segment.update(start_ms=0, end_ms=4000)
        report = cross_source_report(canonical)
        self.assertEqual(report['overlap_ms'], 4000)
        self.assertFalse(report['suspicious'])

    def test_long_meeting_does_not_dilute_duplicate_evidence(self):
        canonical = self.duplicated()
        for i in range(100):
            canonical['segments'].append({'id': f'other{i}', 'text': f'Otra intervención {i}',
                                         'start_ms': 40000+i*100, 'end_ms': 40100+i*100, 'source_id': 'system'})
        self.assertTrue(cross_source_report(canonical)['suspicious'])

    def test_cross_source_fallback_has_unknown_speakers_and_missing_live_blocks(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); task = self.closed(root); canonical = self.duplicated()
            batch = {source: {**canonical, 'segments': [s for s in canonical['segments'] if s['source_id'] == source]}
                     for source in ['microphone', 'system']}
            chosen, report = select_canonical(batch, canonical, task, root)
            self.assertEqual(report['reason'], 'batch_cross_source_duplication')
            self.assertTrue(all(s['source_id'] == 'live_mix' and s['speaker_id'] is None for s in chosen['segments']))
            (root/'meeting/transcripts.json').unlink()
            with self.assertRaisesRegex(ValueError, 'batch_cross_source_duplication'):
                select_canonical(batch, canonical, task, root)

    def test_local_loop_in_live_backup_cannot_replace_failed_batch(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); task = self.closed(root)
            segments = [{'text': 'Esta frase se está repitiendo una y otra vez',
                         'audio_start_time': i*2, 'audio_end_time': i*2+2} for i in range(10)]
            segments += [{'text': f'Contenido variado {i}', 'audio_start_time': 20+i, 'audio_end_time': 21+i} for i in range(30)]
            (root/'meeting/transcripts.json').write_text(json.dumps({'segments': segments}))
            batch = self.batch()
            with self.assertRaisesRegex(ValueError, 'batch_transcription_repetitive'):
                select_canonical({'microphone': batch}, batch, task, root)

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
