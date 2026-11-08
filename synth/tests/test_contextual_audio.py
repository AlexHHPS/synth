import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
import wave

import numpy as np

from synth.worker.consolidation import ConsolidationJournal, RATE
from synth.worker.contextual_audio import ContextualProcessor
from synth.worker.incremental_audio import SourceReader


class ContextualAudioTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name);(self.root/'sources').mkdir()
        self.capture=str(uuid4());self.calls=[];self.observed=[]
        self.journal=ConsolidationJournal(self.root/'state/cores.sqlite3',self.capture,'synthetic-model')
        self.addCleanup(self.journal.close)
        for source in ('microphone','system'):
            with wave.open(str(self.root/'sources'/f'{source}.wav.part'),'wb') as audio:
                audio.setparams((1,2,RATE,0,'NONE','not compressed'));audio.writeframes(np.full(24*RATE,8192,dtype='<i2').tobytes())

    def progress(self,frames=24*RATE,state='closed'):
        (self.root/'sources/progress.json').write_text(json.dumps({'version':1,'capture_id':self.capture,
            'state':state,'clock':'recording_elapsed','sample_rate':RATE,'sample_format':'pcm16',
            'sources':{'microphone':frames,'system':frames}}))

    def prepare(self,signals):
        return signals,{s:{'has_speech':True} for s in signals}

    def decode(self,source,audio,start,end):
        self.calls.append((source,start,end,len(audio)))
        return {'text':'No.'}

    def align(self,source,audio,text,start):
        return [{'text':text,'start_ms':11920 if start==0 else 11880,'end_ms':12080 if start==0 else 12040}]

    def processor(self,decode=None,align=None):
        return ContextualProcessor(SourceReader(self.root),self.journal,decode or self.decode,align or self.align,self.prepare,
            lambda signals,start,end:self.observed.append((start,end,{s:len(a) for s,a in signals.items()})))

    def drain(self,processor):
        for _ in range(30):
            result=processor.poll()
            if result['state']=='drained':return result
        self.fail('Synthetic capture did not drain')

    def test_once_per_context_window_with_bounded_buffers_and_exact_tail(self):
        self.progress();processor=self.processor();self.drain(processor)
        self.assertEqual([(s,a,b) for s,a,b,_ in self.calls],
            [('microphone',0.,15.),('system',0.,15.),('microphone',9.,24.),('system',9.,24.)])
        self.assertTrue(all(n<=18*16000 for *_,n in self.calls))
        self.assertTrue(all(len(a)<=18*16000 for a in processor.buffers.values()))
        self.assertEqual(len(self.observed),8)
        for source in ('microphone','system'):
            cores=self.journal.cores(source)
            self.assertEqual(' '.join(c['result']['text'] for c in cores).split(),['No.'])
            self.assertTrue(all(c['revision']==2 for c in cores))

    def test_replay_validates_inputs_without_repeating_completed_model_work(self):
        self.progress();self.drain(self.processor())
        events=self.journal.events()
        def forbidden(*args):self.fail('Completed inference was repeated')
        self.drain(self.processor(forbidden,forbidden))
        self.assertEqual(events,self.journal.events())

    def test_alignment_interruption_preserves_decode_but_requires_new_preprocessing(self):
        self.progress()
        def interrupted(*args):raise RuntimeError('Synthetic aligner interruption')
        processor=self.processor(align=interrupted)
        with self.assertRaises(RuntimeError):self.drain(processor)
        self.assertEqual(len(self.calls),1)
        with self.assertRaisesRegex(ValueError,'consolidation_restart_required'):processor.poll()
        self.drain(self.processor())
        self.assertEqual(len(self.calls),4)  # four windows total; failed alignment did not add a decode

    def test_uncommitted_lookahead_cannot_finalize_a_core(self):
        self.progress(frames=13*RATE,state='open');processor=self.processor()
        for _ in range(8):processor.poll()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.journal.events(through_frame=processor.verified_context_frame),[])
        self.progress(frames=16*RATE,state='open')
        for _ in range(3):processor.poll()
        self.assertEqual(len(self.calls),2)
        self.assertTrue(all(e['result']['state']=='provisional' for e in self.journal.events(through_frame=processor.verified_context_frame)))

    def test_changed_source_cannot_reuse_future_cached_words(self):
        self.progress();self.drain(self.processor())
        with (self.root/'sources/system.wav.part').open('r+b') as source:
            source.seek(44+20*RATE*2);source.write(b'\0\0'*1000)
        processor=self.processor()
        with self.assertRaisesRegex(ValueError,'consolidation_audio_changed'):self.drain(processor)
        visible=self.journal.events(through_frame=processor.verified_context_frame)
        self.assertEqual([e['sequence'] for e in visible],[1,2])
