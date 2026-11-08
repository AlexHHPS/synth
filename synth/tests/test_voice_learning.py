"""Counterexamples for rooted learning, not acoustic accuracy claims."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from pydantic import ValidationError
from synth.contracts.voice_learning import Sample
from synth.speakers.learning import Outbox, samples_from_contexts
from synth.speakers.reference_session import ReferenceSession


def basis(size, index=0):
    return [float(i==index) for i in range(size)]


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.owner,self.capture,self.profile=str(uuid4()),str(uuid4()),str(uuid4())
        self.model='a'*40
        self.identities=[{'id':self.profile,'employee_id':self.owner,'name':'Synthetic person',
                          'model':self.model,'vector':basis(256),'shared':True}]
        self.legacy=[{**self.identities[0],'vector':basis(128)}]

    def context(self,index,rho=None,raw=None):
        common={'capture_id':self.capture,'owner':self.owner,'source':'system',
                'model_lock_sha256':'b'*64,'policy':'test'}
        start=index*160000
        five=[{**common,'id':hashlib.sha256(f'{index}-{i}'.encode()).hexdigest(),
               'model':self.model,'crop_seconds':5,'ranges':[[start+i*80000,start+(i+1)*80000]],
               'rho128':rho or basis(128)} for i in range(2)]
        ten={**common,'id':hashlib.sha256(f'{index}-ten'.encode()).hexdigest(),'model':self.model+'#crop10s',
             'crop_seconds':10,'ranges':[r for o in five for r in o['ranges']],'embedding':raw or basis(256)}
        return {'group_id':f'system:{start}:S1','observations':[*five,ten]}

    def outcomes(self,contexts):
        session=ReferenceSession(self.legacy,self.model,identities=self.identities)
        return [session.observe(c['group_id'],c['observations']) for c in contexts]

    def generate(self,contexts,outcomes=None,identities=None):
        return samples_from_contexts(contexts,outcomes or self.outcomes(contexts),
                                     identities or self.identities,self.legacy,self.model)

    def test_strong_independent_votes_export_only_crop10_and_no_names_or_audio(self):
        context=self.context(0)
        sample=self.generate([context])[0]
        self.assertEqual(Sample.model_validate(sample).origin,'enrolled_high_confidence')
        self.assertEqual(sample['vote_crop_seconds'],5)
        self.assertEqual(sample['source_ranges'],[[0,80000],[80000,160000]])
        self.assertNotIn('Synthetic person',json.dumps(sample))
        self.assertNotIn('audio',sample)
        self.assertEqual(self.generate([context]),[sample])

    def test_fallback_and_unshared_profiles_cannot_train_shared_samples(self):
        contexts=[self.context(0),self.context(1,rho=basis(128,2))]
        outcomes=self.outcomes(contexts)
        self.assertEqual(outcomes[1]['reason'],'session_reference')
        self.assertEqual(len(self.generate(contexts,outcomes)),1)
        self.assertEqual(self.generate(contexts,outcomes,[{**self.identities[0],'shared':False}]),[])

    def test_overlapping_echo_and_duplicate_poll_do_not_add_more_votes(self):
        context=self.context(0)
        self.assertEqual(len(self.generate([context,context])),1)
        bad=self.context(1);bad['observations'][1]['ranges']=bad['observations'][0]['ranges']
        self.assertEqual(self.generate([bad]),[])

    def test_derived_profile_cannot_be_an_enrollment_anchor(self):
        derived=[{**self.identities[0],'derived':True}]
        self.assertEqual(self.generate([self.context(0)],identities=derived),[])

    def test_new_crop10_enrollment_needs_two_disjoint_strong_windows(self):
        identities=[{**self.identities[0],'model':self.model+'#crop10s'}]
        contexts=[self.context(0,rho=basis(128,2)),self.context(1,rho=basis(128,2))]
        outcomes=[{'state':'suggested','profile_id':self.profile,'candidate_employee_id':self.owner} for _ in contexts]
        self.assertEqual(samples_from_contexts(contexts[:1],outcomes[:1],identities,[],self.model),[])
        samples=samples_from_contexts(contexts,outcomes,identities,[],self.model)
        self.assertEqual(len(samples),2)
        self.assertTrue(all(Sample.model_validate(s).vote_crop_seconds==10 for s in samples))

    def test_confirmation_is_a_distinct_auditable_sample_and_not_a_machine_identity(self):
        context=self.context(0)
        outcome={'state':'confirmed','confirmed':True,'candidate_employee_id':self.owner,
                 'profile_id':self.profile,'confirmation_id':str(uuid4())}
        sample=self.generate([context],[outcome])[0]
        self.assertEqual(Sample.model_validate(sample).origin,'human_confirmation')
        self.assertNotEqual(sample['id'],self.generate([context])[0]['id'])

    def test_contract_rejects_audio_weak_votes_duplicate_votes_and_invalid_ranges(self):
        sample=self.generate([self.context(0)])[0]
        changes=[{'audio':'never accepted'},{'vote_similarities':[.89,.99]},
                 {'vote_evidence_ids':[sample['evidence_ids'][0]]*2},
                 {'source_ranges':[[0,80000],[0,80000]]},{'source_ranges':[[0,150000]]}]
        for change in changes:
            with self.subTest(change=list(change)),self.assertRaises(ValidationError):
                Sample.model_validate({**sample,**change})

    def test_outbox_is_encrypted_owner_fenced_idempotent_and_retraction_wins_upload_race(self):
        key=os.urandom(32);sample=self.generate([self.context(0)])[0]
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'learning.sqlite3';store=Outbox(path,key)
            try:
                self.assertTrue(store.enqueue(self.owner,sample));self.assertFalse(store.enqueue(self.owner,sample))
                self.assertEqual(store.pending(str(uuid4())),[])
                self.assertEqual(store.pending(self.owner)[0][2]['embedding'],sample['embedding'])
                self.assertNotIn(b'"embedding"',path.read_bytes())
                self.assertEqual(path.stat().st_mode&0o777,0o600)
                store.retract_group(self.owner,self.capture,'system:0:S1')
                self.assertFalse(store.acknowledge(sample['id'],'accepted'))
                self.assertEqual(store.pending(self.owner)[0][1],'revoke_queued')
                self.assertTrue(store.acknowledge(sample['id'],'rejected',expected='revoke_queued'))
                self.assertEqual(store.pending(self.owner),[])
                row=store.db.execute('SELECT * FROM samples').fetchone()
                plaintext=store.key.decrypt(row['nonce'],row['ciphertext'],(self.owner+':'+sample['id']).encode())
                self.assertEqual(plaintext,b'{}')
            finally:store.close()

    def test_later_strong_samples_expand_references_but_fallback_does_not(self):
        session=ReferenceSession(self.legacy,self.model,identities=self.identities)
        for index,raw in [(0,basis(256)),(1,basis(256,1))]:
            context=self.context(index,raw=raw);session.observe(context['group_id'],context['observations'])
        later=self.context(2,rho=basis(128,2),raw=basis(256,1))
        outcome=session.observe(later['group_id'],later['observations'])
        self.assertEqual(outcome['reason'],'session_reference')
        self.assertEqual(outcome['reference_evidence_ids'][-1],self.context(1)['observations'][-1]['id'])
        self.assertEqual(len(session.reference_pool()),2)

    def test_conflicting_crop_policies_do_not_seed_a_session_reference(self):
        session=ReferenceSession(self.legacy,self.model,identities=self.identities)
        context=self.context(0)
        outcome=session.observe(context['group_id'],context['observations'],competing_candidate=str(uuid4()))
        self.assertEqual(outcome['reason'],'crop_policy_disagreement')
        self.assertEqual(session.reference_pool(),[])

    def test_two_approved_ten_second_matches_calibrate_but_one_or_echo_cannot(self):
        profiles=[{**self.identities[0],'model':self.model+'#crop10s'}]
        session=ReferenceSession([],self.model,identities=profiles)
        first=self.context(0,rho=basis(128,2),raw=[.8,.6]+[0.]*254)
        session.observe_crop10(first['group_id'],first['observations'],profiles)
        self.assertEqual(session.reference_pool(),[])
        session.observe_crop10('echo',first['observations'],profiles)
        self.assertEqual(session.reference_pool(),[])
        second=self.context(1,rho=basis(128,2),raw=[.8,.6]+[0.]*254)
        session.observe_crop10(second['group_id'],second['observations'],profiles)
        self.assertEqual(len(session.reference_pool()),2)
        weak=self.context(2,rho=basis(128,2),raw=[.65,(1-.65**2)**.5]+[0.]*254)
        result=session.observe(weak['group_id'],weak['observations'])
        self.assertEqual(result['reason'],'session_reference')
        self.assertFalse(result['confirmed'])
        session.observe_crop10(weak['group_id'],weak['observations'],profiles)
        self.assertEqual(len(session.reference_pool()),2)
        # Session calibration is separate from durable enrichment. A local
        # fallback must not acquire an invented enrollment-quality score.
        self.assertEqual(samples_from_contexts([weak],[result],profiles,[],self.model),[])

    def test_conflicting_strong_legacy_vote_blocks_ten_second_calibration(self):
        other={**self.identities[0],'id':str(uuid4()),'employee_id':str(uuid4()),'model':self.model+'#crop10s'}
        session=ReferenceSession(self.legacy,self.model,identities=[*self.identities,other])
        for index in (0,1):
            context=self.context(index)
            session.observe_crop10(context['group_id'],context['observations'],[other])
        self.assertEqual(session.reference_pool(),[])

    def test_human_correction_removes_a_secondary_prototype_from_old_person(self):
        other={**self.identities[0],'id':str(uuid4()),'employee_id':str(uuid4()),'name':'Another synthetic person'}
        session=ReferenceSession(self.legacy,self.model,identities=[*self.identities,other])
        for index in (0,1):
            context=self.context(index);session.observe(context['group_id'],context['observations'])
        context=self.context(1)
        session.confirm(context['group_id'],context['observations'],other['id'],str(uuid4()))
        old=[r for r in session.reference_pool() if r['employee_id']==self.owner]
        self.assertEqual(len(old),1)
        self.assertEqual(old[0]['evidence_ids'][-1],self.context(0)['observations'][-1]['id'])
