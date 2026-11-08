import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from synth.worker.consolidation import ConsolidationJournal, RATE


def frames(seconds):return round(seconds*RATE)
def word(text,start,end):return {'text':text,'start_ms':start,'end_ms':end}
DIGEST=hashlib.sha256(b'public synthetic source').hexdigest()


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/'journal.sqlite3';self.capture=str(uuid4())
        self.journal=ConsolidationJournal(self.path,self.capture,'test-model-context-v1')
        self.addCleanup(lambda:self.journal.close())

    def store(self,start,end,left,right,words,source='microphone',closed=False):
        self.journal.begin(source,frames(start),frames(end),frames(left),frames(right),DIGEST,closed=closed)
        self.journal.decoded(source,frames(start),{'text':' '.join(w['text'] for w in words),'speech_presence':{'has_speech':True}})
        self.journal.aligned(source,frames(start),words)
        return self.journal.commit(source,frames(start))

    def test_measured_no_boundary_jitter_becomes_one_recoverable_core_revision(self):
        self.store(0,12,0,15,[word('No.',11920,12080)])
        self.assertEqual(self.journal.events()[0]['result']['text'],'No.')
        self.assertEqual(self.journal.events()[0]['result']['state'],'provisional')
        self.store(12,24,9,24,[word('No.',11880,12040)],closed=True)
        self.journal.finish('microphone',frames(24))
        cores=self.journal.cores('microphone')
        self.assertEqual(' '.join(c['result']['text'] for c in cores).split(),['No.'])
        self.assertTrue(all(c['revision']==2 for c in cores))
        self.assertTrue(all(c['result']['state']=='aligned' for c in cores))
        self.assertEqual([e['sequence'] for e in self.journal.events()],[1,2,3,4])
        self.assertFalse(self.journal.commit('microphone',0))
        self.assertFalse(self.journal.finish('microphone',frames(24)))
        self.assertEqual(len(self.journal.events()),4)

    def test_completed_decode_survives_alignment_failure_and_cannot_be_rewritten(self):
        self.journal.begin('system',0,frames(12),0,frames(15),DIGEST)
        original={'text':'No está aprobado.','generation_tokens':7}
        self.journal.decoded('system',0,original)
        self.journal.close()
        self.journal=ConsolidationJournal(self.path,self.capture,'test-model-context-v1')
        cached=self.journal.begin('system',0,frames(12),0,frames(15),DIGEST)
        self.assertEqual(cached['decoded'],original);self.assertIsNone(cached['words'])
        with self.assertRaisesRegex(ValueError,'consolidation_alignment_text_changed'):
            self.journal.aligned('system',0,[word('Está aprobado.',100,1000)])
        with self.assertRaisesRegex(ValueError,'consolidation_decode_immutable'):
            self.journal.decoded('system',0,{'text':'Sí está aprobado.'})
        with self.assertRaisesRegex(ValueError,'consolidation_audio_changed'):
            self.journal.begin('system',0,frames(12),0,frames(15),'0'*64)

    def test_future_cached_events_wait_for_their_validated_context(self):
        self.store(0,12,0,15,[word('Antes.',1000,2000)])
        self.store(12,24,9,27,[word('Después.',20000,21000)])
        self.assertEqual(self.journal.events(through_frame=frames(12)),[])
        self.assertEqual([e['sequence'] for e in self.journal.events(through_frame=frames(15))],[1])
        self.assertEqual([e['sequence'] for e in self.journal.events(after=1,through_frame=frames(27))],[2,3])
        with self.assertRaisesRegex(ValueError,'consolidation_tail_unobserved'):
            self.journal.finish('microphone',frames(24))

    def test_failed_seam_commit_is_atomic_and_can_resume_without_decoding_again(self):
        self.store(0,12,0,15,[word('No.',11920,12080)])
        original=self.journal._emit
        calls=[]
        def interrupted(*args,**kwargs):
            calls.append(True)
            if len(calls)==2:raise RuntimeError('Synthetic process interruption')
            return original(*args,**kwargs)
        with patch.object(self.journal,'_emit',side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.store(12,24,9,24,[word('No.',11880,12040)],closed=True)
        self.assertEqual(len(self.journal.events()),1)
        self.assertEqual(self.journal.cores('microphone')[0]['revision'],1)
        self.assertIsNotNone(self.journal.begin('microphone',frames(12),frames(24),frames(9),frames(24),DIGEST,closed=True)['decoded'])
        self.assertTrue(self.journal.commit('microphone',frames(12)))
        self.journal.finish('microphone',frames(24))
        self.assertEqual(len(self.journal.events()),4)

    def test_sources_remain_separate_even_when_the_words_and_times_match(self):
        for source in ('microphone','system'):
            self.store(0,12,0,12,[word('No.',100,300)],source=source,closed=True)
        for source in ('microphone','system'):
            self.journal.finish(source,frames(12))
            core=self.journal.cores(source)[0]
            self.assertEqual(core['result']['source'],source)
            self.assertNotIn('employee_id',core['result'])
        self.assertEqual(len(self.journal.events()),4)

    def test_capture_policy_and_uncommitted_context_are_fenced(self):
        with self.assertRaisesRegex(ValueError,'consolidation_context_not_ready'):
            self.journal.begin('microphone',0,frames(12),0,frames(13),DIGEST)
        with self.assertRaisesRegex(ValueError,'consolidation_policy_changed'):
            ConsolidationJournal(self.path,str(uuid4()),'test-model-context-v1')
        with self.assertRaisesRegex(ValueError,'consolidation_policy_changed'):
            ConsolidationJournal(self.path,self.capture,'changed-model')
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)

    def test_unknown_voice_evidence_keeps_the_negation_and_review_state(self):
        self.journal.begin('microphone',0,frames(1),0,frames(1),DIGEST,closed=True)
        self.journal.decoded('microphone',0,{'text':'No.','speech_presence':{'has_speech':None}})
        self.journal.aligned('microphone',0,[word('No.',100,300)])
        self.journal.commit('microphone',0);self.journal.finish('microphone',frames(1))
        core=self.journal.cores('microphone')[0]['result']
        self.assertEqual((core['text'],core['state']),('No.','needs_review'))
