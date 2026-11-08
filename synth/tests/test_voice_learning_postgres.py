"""Real PostgreSQL: profile preservation, subject approval, revocation and RLS."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from synth.server.db import authenticate, bind_actor, digest
from synth.server.voice_registry import Enrollment, seal, unseal
from synth.server.voice_learning import Sample, submit, accepted_profiles


@unittest.skipUnless(os.environ.get('SYNTH_TEST_DATABASE_URL'),'Disposable PostgreSQL URL required')
class VoiceLearningPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.psycopg,self.dict_row=psycopg,dict_row
        self.url=os.environ['SYNTH_TEST_DATABASE_URL'];self.assertIn('@127.0.0.1:18289/',self.url)
        self.enterContext(patch.dict(os.environ,{'AUTH_MODE':'keys','VOICE_PROFILE_ENCRYPTION_KEY':base64.b64encode(b'x'*32).decode()}))
        self.schema='voice_learning_'+uuid4().hex
        self.admin=psycopg.connect(self.url,autocommit=True,row_factory=dict_row)
        self.admin.execute("""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='synth_voice_api') THEN CREATE ROLE synth_voice_api NOLOGIN; END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='synth_voice_worker') THEN CREATE ROLE synth_voice_worker NOLOGIN; END IF;
            END $$""")
        self.admin.execute('CREATE SCHEMA '+self.schema);self.admin.execute('SET search_path='+self.schema)
        schema=(Path(__file__).resolve().parents[1]/'server/schema.sql').read_text()
        self.admin.execute(schema);self.admin.execute(schema)
        self.people,self.tokens={},{}
        for name in ('subject','contributor','other'):
            person,token=uuid4(),'synthetic-'+uuid4().hex;self.people[name],self.tokens[name]=person,token
            self.admin.execute("INSERT INTO principals(id,name,kind) VALUES (%s,%s,'user')",(person,name))
            self.admin.execute('INSERT INTO access_keys(id,principal_id,token_hash) VALUES (%s,%s,%s)',(uuid4(),person,digest(token)))
            self.admin.execute("INSERT INTO auth_identities(provider,subject,principal_id) VALUES ('supabase',%s,%s)",(uuid4(),person))
        self.root=uuid4();self.model='a'*40
        enrollment=Enrollment(id=self.root,model=self.model,embedding=[1.]+[0.]*255,consent_confirmed=True)
        nonce,sealed=seal(enrollment,self.people['subject']);self.original_ciphertext=sealed
        self.admin.execute('INSERT INTO voice_profiles(id,owner_id,model,nonce,ciphertext) VALUES (%s,%s,%s,%s,%s)',
                           (self.root,self.people['subject'],self.model,nonce,sealed))

    def tearDown(self):
        self.admin.execute('DROP SCHEMA '+self.schema+' CASCADE');self.admin.close()

    @contextmanager
    def api(self,name):
        with self.psycopg.connect(self.url,row_factory=self.dict_row) as db:
            db.execute('SET search_path='+self.schema);db.execute('SET ROLE synth_voice_api')
            actor=authenticate(db,self.tokens[name]);bind_actor(db,actor)
            yield db,actor

    def sample(self):
        evidence=[hashlib.sha256(uuid4().bytes).hexdigest() for _ in range(3)]
        return Sample(id=uuid4(),root_profile_id=self.root,model=self.model+'#crop10s',embedding=[1.]+[0.]*255,
            consent_confirmed=True,capture_id=uuid4(),group_id='c'*64,source='microphone',source_ranges=[[0,160000]],
            evidence_ids=evidence,origin='enrolled_high_confidence',vote_similarities=[.97,.98],vote_margins=[.2,.3],
            vote_evidence_ids=evidence[:2],vote_crop_seconds=5,policy='rooted_independent_crop10_v1')

    def test_own_sample_is_additive_encrypted_and_idempotent(self):
        body=self.sample()
        with self.api('subject') as (db,actor):
            self.assertEqual(submit(db,actor,body)['state'],'accepted')
            self.assertEqual(submit(db,actor,body)['state'],'accepted')
            rows=accepted_profiles(db);self.assertEqual(len(rows),1)
            self.assertEqual(unseal(rows[0]),[1.]+[0.]*255)
        self.assertEqual(self.admin.execute('SELECT count(*) AS n FROM voice_profile_samples').fetchone()['n'],1)
        self.assertEqual(bytes(self.admin.execute('SELECT ciphertext FROM voice_profiles WHERE id=%s',(self.root,)).fetchone()['ciphertext']),self.original_ciphertext)

    def test_coworker_contribution_is_pending_hidden_from_others_and_cannot_self_accept(self):
        body=self.sample()
        with self.api('contributor') as (db,actor):
            self.assertEqual(submit(db,actor,body)['state'],'pending')
            self.assertEqual(accepted_profiles(db),[])
        with self.api('other') as (db,_):
            self.assertEqual(db.execute('SELECT id FROM voice_profile_samples').fetchall(),[])
        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
            with self.api('contributor') as (db,_):
                db.execute("UPDATE voice_profile_samples SET state='accepted' WHERE id=%s",(body.id,))
        with self.api('subject') as (db,_):
            db.execute("UPDATE voice_profile_samples SET state='accepted' WHERE id=%s",(body.id,))
        with self.api('other') as (db,_):
            self.assertEqual(len(accepted_profiles(db)),1)

    def test_rejection_erases_vector_and_replay_cannot_reactivate_it(self):
        body=self.sample()
        with self.api('contributor') as (db,actor):submit(db,actor,body)
        with self.api('subject') as (db,_):
            db.execute("UPDATE voice_profile_samples SET state='rejected',nonce=NULL,ciphertext=NULL WHERE id=%s",(body.id,))
        with self.api('contributor') as (db,actor):
            self.assertEqual(submit(db,actor,body)['state'],'rejected')
            self.assertEqual(accepted_profiles(db),[])
        row=self.admin.execute('SELECT nonce,ciphertext FROM voice_profile_samples WHERE id=%s',(body.id,)).fetchone()
        self.assertIsNone(row['nonce']);self.assertIsNone(row['ciphertext'])

    def test_root_revocation_cascades_and_document_worker_cannot_read_vectors(self):
        with self.api('subject') as (db,actor):submit(db,actor,self.sample())
        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
            with self.psycopg.connect(self.url) as db:
                db.execute('SET search_path='+self.schema);db.execute('SET ROLE synth_voice_worker');db.execute('SELECT ciphertext FROM voice_profile_samples')
        with self.api('subject') as (db,_):db.execute('DELETE FROM voice_profiles WHERE id=%s',(self.root,))
        self.assertEqual(self.admin.execute('SELECT count(*) AS n FROM voice_profile_samples').fetchone()['n'],0)

    def test_adaptive_sample_cannot_be_the_root_of_another_shared_sample(self):
        first=self.sample()
        with self.api('subject') as (db,actor):submit(db,actor,first)
        body=self.sample().model_copy(update={'root_profile_id':first.id})
        with self.api('subject') as (db,actor),self.assertRaises(HTTPException) as failure:submit(db,actor,body)
        self.assertEqual(failure.exception.status_code,404)

    def test_two_contributors_share_capacity_without_exposing_each_others_pending_data(self):
        for _ in range(63):
            with self.api('subject') as (db,actor):submit(db,actor,self.sample())
        def contribute(name):
            try:
                with self.api(name) as (db,actor):return submit(db,actor,self.sample())['state']
            except HTTPException as error:return error.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:outcomes=list(pool.map(contribute,['contributor','other']))
        self.assertCountEqual(outcomes,['pending',409])
        self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM voice_profile_samples WHERE state<>'rejected'").fetchone()['n'],64)
