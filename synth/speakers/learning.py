"""Optional encrypted outbox for independently rooted voice samples.

ASR never depends on this outbox. An inferred session match cannot be a new
enrollment anchor; pending coworker samples never enter the shared catalog.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import UUID, uuid5, NAMESPACE_URL

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .reference_session import pair
from .matching_legacy import PILOT_THRESHOLD, PILOT_MARGIN
from .profiles import normalized
from synth.worker.voice_binding import overlap

POLICY = 'rooted_independent_crop10_v1'
_SYNC = threading.Lock()


def vote(vector, profiles):
    scores = {}
    for profile in profiles:
        person = profile['employee_id']
        scores[person] = max(scores.get(person,-1), sum(a*b for a,b in zip(vector,profile['vector'])))
    ranked = sorted(scores, key=lambda person:-scores[person])
    if not ranked:
        return None
    person = ranked[0]
    return person, scores[person], scores[person]-(scores[ranked[1]] if len(ranked)>1 else -1)


def samples_from_contexts(contexts, outcomes, profiles, legacy, model):
    """Use root enrollments or explicit human decisions, never inferred children."""
    originals = [p for p in profiles if not p.get('derived')]
    shared = [p for p in originals if p.get('shared') and p['model'] in (model,model+'#crop10s')]
    legacy = [p for p in legacy if not p.get('derived')]
    ten_profiles = [p for p in originals if p['model']==model+'#crop10s']
    records = []
    for context,outcome in zip(contexts,outcomes):
        five,ten = pair(context['observations'],model)
        if ten is None or outcome['state'] not in ('suggested','confirmed'):
            continue
        person = outcome.get('candidate_employee_id')
        root = next((p for p in shared if p['employee_id']==person and p['id']==outcome.get('profile_id')),
                    next((p for p in shared if p['employee_id']==person),None))
        if root is None:
            continue
        record = {'context':context,'outcome':outcome,'five':five,'ten':ten,'person':person,'root':root}
        if outcome.get('confirmed'):
            record.update(origin='human_confirmation',votes=[],vote_ids=[],vote_seconds=10)
        else:
            votes = [vote(normalized(o['rho128'],128),legacy) for o in five]
            if all(v and v[0]==person and v[1]>=PILOT_THRESHOLD and v[2]>=PILOT_MARGIN for v in votes):
                record.update(origin='enrolled_high_confidence',votes=votes,
                              vote_ids=[o['id'] for o in five],vote_seconds=5)
            else:
                direct = vote(normalized(ten['embedding']),ten_profiles)
                if not (direct and direct[0]==person and direct[1]>=.90 and direct[2]>=.10):
                    continue
                record.update(origin='enrolled_high_confidence',votes=[direct],vote_ids=[ten['id']],vote_seconds=10)
        records.append(record)
    result, used = [], {}
    for record in records:
        ten, person = record['ten'], record['person']
        if record['vote_seconds']==10 and record['origin']=='enrolled_high_confidence':
            peer = next((r for r in records if r is not record and r['person']==person
                         and r['vote_seconds']==10 and r['origin']=='enrolled_high_confidence'
                         and not overlap(r['ten']['ranges'],ten['ranges'])),None)
            if peer is None:
                continue
            votes, vote_ids = record['votes']+peer['votes'], record['vote_ids']+peer['vote_ids']
        else:
            votes, vote_ids = record['votes'], record['vote_ids']
        ranges = used.setdefault(person,[])
        if len([r for r in result if r['root_profile_id']==record['root']['id']])>=4 or overlap(ranges,ten['ranges']):
            continue
        ranges.extend(ten['ranges'])
        provenance = record['outcome'].get('confirmation_id') or 'enrolled'
        identifier = str(uuid5(NAMESPACE_URL,POLICY+':'+ten['capture_id']+':'+ten['id']+':'+record['root']['id']+':'+provenance))
        result.append({'id':identifier,'root_profile_id':record['root']['id'],'model':ten['model'],
            'embedding':normalized(ten['embedding']),'capture_id':ten['capture_id'],
            'group_id':hashlib.sha256(record['context']['group_id'].encode()).hexdigest(),
            'source':ten['source'],'source_ranges':ten['ranges'],
            'evidence_ids':[o['id'] for o in record['five']]+[ten['id']],
            'origin':record['origin'],'vote_similarities':[v[1] for v in votes],
            'vote_margins':[v[2] for v in votes],'vote_evidence_ids':vote_ids,
            'vote_crop_seconds':record['vote_seconds'],'confirmation_id':record['outcome'].get('confirmation_id'),
            'policy':POLICY,'consent_confirmed':True})
    return result


def enabled(actor, configuration=None):
    from synth.worker.qwen_asr import CONFIG
    try:
        config = configuration if configuration is not None else json.loads(CONFIG.read_text())
        return config.get('voice_learning',{}).get(str(actor['id']),{}).get('enabled') is True
    except (OSError,ValueError,AttributeError):
        return False


class Outbox:
    def __init__(self, path, key):
        path = Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if path.is_symlink():
            raise ValueError('voice_learning_path_invalid')
        self.db=sqlite3.connect(path,timeout=5);path.chmod(0o600);self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('''CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY,owner TEXT NOT NULL,
            nonce BLOB NOT NULL,ciphertext BLOB NOT NULL,state TEXT NOT NULL,updated REAL NOT NULL,
            capture_id TEXT NOT NULL,group_hash TEXT NOT NULL,root_profile_id TEXT NOT NULL)''')
        self.key=AESGCM(key)

    def close(self):
        self.db.close()

    def enqueue(self, owner, sample):
        from synth.contracts.voice_learning import Sample
        value=Sample.model_validate(sample).model_dump(mode='json')
        owner=str(UUID(owner));identifier=value['id']
        if self.db.execute('SELECT 1 FROM samples WHERE id=?',(identifier,)).fetchone():
            return False
        if self.db.execute("SELECT count(*) FROM samples WHERE owner=? AND state='queued'",(owner,)).fetchone()[0]>=128:
            return False
        nonce=os.urandom(12);aad=(owner+':'+identifier).encode()
        encrypted=self.key.encrypt(nonce,json.dumps(value,sort_keys=True).encode(),aad)
        with self.db:self.db.execute('INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?,?,?,?,?)',
                                    (identifier,owner,nonce,encrypted,'queued',time.time(),
                                     value['capture_id'],value['group_id'],value['root_profile_id']))
        return True

    def pending(self, owner):
        rows=self.db.execute("SELECT * FROM samples WHERE owner=? AND state IN ('queued','revoke_queued') ORDER BY state DESC,updated LIMIT 8",(str(owner),)).fetchall()
        return [(row['id'],row['state'],json.loads(self.key.decrypt(row['nonce'],row['ciphertext'],(row['owner']+':'+row['id']).encode()))) for row in rows]

    def acknowledge(self, identifier, state, expected='queued'):
        if state not in ('accepted','pending','rejected','unavailable'):
            raise ValueError('voice_learning_response_invalid')
        with self.db:
            changed=self.db.execute('UPDATE samples SET state=?,updated=? WHERE id=? AND state=?',(state,time.time(),identifier,expected)).rowcount
            if changed and state in ('rejected','unavailable'):
                row=self.db.execute('SELECT owner FROM samples WHERE id=?',(identifier,)).fetchone()
                nonce=os.urandom(12);sealed=self.key.encrypt(nonce,b'{}',(row['owner']+':'+identifier).encode())
                self.db.execute('UPDATE samples SET nonce=?,ciphertext=? WHERE id=?',(nonce,sealed,identifier))
        return bool(changed)

    def retract_group(self, owner, capture_id, group_id, keep_roots=()):
        digest=hashlib.sha256(group_id.encode()).hexdigest()
        rows=self.db.execute("SELECT id,root_profile_id FROM samples WHERE owner=? AND capture_id=? AND group_hash=? AND state NOT IN ('rejected','unavailable','revoke_queued')",(str(owner),capture_id,digest)).fetchall()
        for row in rows:
            if row['root_profile_id'] not in keep_roots:
                with self.db:self.db.execute("UPDATE samples SET state='revoke_queued',updated=? WHERE id=?",(time.time(),row['id']))

    def counts(self, owner):
        return {r['state']:r['n'] for r in self.db.execute('SELECT state,count(*) AS n FROM samples WHERE owner=? GROUP BY state',(str(owner),))}


def open_outbox():
    from .onboarding import ROOT
    from .keychain import obtain
    return Outbox(ROOT/'learning.sqlite3',obtain(ROOT/'vault-key'))


def synchronize(actor):
    if not _SYNC.acquire(blocking=False):
        return
    try:
        from synth.worker.desktop_pipeline import LocalAPI
        api=LocalAPI();api.expected_principal=str(actor['id'])
        if api.auth_mode!='supabase':
            return
        store=open_outbox()
        try:
            for identifier,operation,sample in store.pending(actor['id']):
                api.assert_current_principal()
                if operation=='queued' and not enabled(actor):
                    continue
                try:
                    if operation=='revoke_queued':
                        api.call('DELETE','/v1/voice/samples/'+identifier)
                        store.acknowledge(identifier,'rejected',expected=operation)
                    else:
                        response=api.call('POST','/v1/voice/samples',sample)
                        if not store.acknowledge(identifier,response['state']):
                            # A human correction raced the upload. Retract the
                            # just-published sample before acknowledging it.
                            api.assert_current_principal()
                            api.call('DELETE','/v1/voice/samples/'+identifier)
                            store.acknowledge(identifier,'rejected',expected='revoke_queued')
                except ValueError as error:
                    if str(error)=='api_http_404':
                        roots=api.call('GET','/v1/voice/profiles')['profiles']
                        if any(p['id']==sample.get('root_profile_id') for p in roots):
                            break  # Rolling API upgrade: retry while the root still exists.
                        store.acknowledge(identifier,'unavailable',expected=operation)
                    elif str(error)=='api_http_422':
                        store.acknowledge(identifier,'unavailable',expected=operation)
                    else:
                        break
        finally:
            store.close()
    except (OSError,ValueError,KeyError,sqlite3.Error):
        pass  # Optional sharing never interrupts ASR, recording or voice suggestions.
    finally:
        _SYNC.release()


def collect(actor, contexts, outcomes, profiles, legacy, model):
    if not enabled(actor):
        return {'enabled':False}
    store=open_outbox()
    try:
        for context,outcome in zip(contexts,outcomes):
            target=outcome.get('candidate_employee_id')
            roots=[p.get('root_profile_id') or p['id'] for p in profiles if target and p['employee_id']==target]
            observations=context['observations']
            if observations:
                store.retract_group(actor['id'],observations[0]['capture_id'],context['group_id'],roots)
        for sample in samples_from_contexts(contexts,outcomes,profiles,legacy,model):
            store.enqueue(actor['id'],sample)
        counts=store.counts(actor['id'])
    finally:
        store.close()
    if counts.get('queued') or counts.get('revoke_queued'):
        threading.Thread(target=synchronize,args=(dict(actor),),daemon=True,name='voice-sample-sync').start()
    return {'enabled':True,'samples':counts}


def retract_capture_group(actor, capture_id, group_id, keep_roots=()):
    from .onboarding import ROOT
    path=ROOT/'learning.sqlite3'
    if not path.is_file():
        return
    with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as connection:
        if not connection.execute('SELECT 1 FROM samples WHERE owner=? LIMIT 1',(str(actor['id']),)).fetchone():
            return
    store=open_outbox()
    try:
        store.retract_group(actor['id'],capture_id,group_id,keep_roots)
    finally:
        store.close()
    threading.Thread(target=synchronize,args=(dict(actor),),daemon=True,name='voice-sample-revoke').start()


def status(actor, api=None):
    from .onboarding import ROOT
    counts={}
    path=ROOT/'learning.sqlite3'
    if path.is_file():
        with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as db:
            counts=dict(db.execute('SELECT state,count(*) FROM samples WHERE owner=? GROUP BY state',(str(actor['id']),)))
    result={'enabled':enabled(actor),'outbox':counts,'samples':[],'sync_state':'unavailable'}
    if api is not None and api.auth_mode=='supabase':
        try:
            page=api.call('GET','/v1/voice/samples');api.assert_current_principal()
            result.update(samples=[{**row,'own':str(row['owner_id'])==str(actor['id'])} for row in page['items']],sync_state='available')
        except (OSError,ValueError,KeyError):
            pass
    return result


def set_enabled(actor, value, api=None):
    if type(value) is not bool:
        raise ValueError('voice_learning_setting_invalid')
    from synth.worker.qwen_asr import CONFIG
    from synth.worker.desktop_pipeline import save_json
    config=json.loads(CONFIG.read_text())
    config.setdefault('voice_learning',{})[str(actor['id'])]={'enabled':value,'updated_at':time.time()}
    save_json(CONFIG,config)
    if value:
        threading.Thread(target=synchronize,args=(dict(actor),),daemon=True,name='voice-sample-sync').start()
    return status(actor,api)
