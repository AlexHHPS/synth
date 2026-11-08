"""Append-only pseudonymous voice samples; no audio, names or cloud comparison.

Only the subject can activate a sample contributed by another person. The source
enrollment remains authoritative and revocation removes all its descendants.
"""
import hashlib
import json
from uuid import UUID

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from .voice_registry import seal
from synth.contracts.voice_learning import Sample
from .db import audit


def submit(db, actor, body):
    if actor['kind'] != 'user' or str(actor['id']) == '00000000-0000-4000-8000-000000000001':
        raise HTTPException(403, 'voice_learning_user_required')
    root = db.execute('SELECT id,owner_id,model FROM voice_profiles WHERE id=%s', (body.root_profile_id,)).fetchone()
    if not root:
        raise HTTPException(404, 'voice_learning_profile_unavailable')
    expected_model = root['model'] if root['model'].endswith('#crop10s') else root['model']+'#crop10s'
    if body.model != expected_model or body.consent_confirmed is not True:
        raise HTTPException(422, 'voice_learning_consent_or_model_invalid')
    db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('voice-learning:'+str(root['owner_id']),))
    encoded = body.model_dump(mode='json')
    fingerprint = hashlib.sha256(json.dumps(encoded, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    old = db.execute('SELECT state,payload_sha256 FROM voice_profile_samples WHERE id=%s AND contributor_id=%s', (body.id, actor['id'])).fetchone()
    if old:
        if old['payload_sha256'] != fingerprint:
            raise HTTPException(409, 'voice_learning_sample_immutable')
        return {'id': body.id, 'state': old['state'], 'audio_stored': False}
    if not db.execute('SELECT voice_sample_capacity(%s) AS allowed',(root['owner_id'],)).fetchone()['allowed']:
        raise HTTPException(409, 'voice_learning_sample_limit')
    state = 'accepted' if str(root['owner_id']) == str(actor['id']) else 'pending'
    nonce, encrypted = seal(body, root['owner_id'])
    evidence = {key: encoded[key] for key in ('capture_id','group_id','source','source_ranges','evidence_ids',
                'origin','vote_similarities','vote_margins','vote_evidence_ids','vote_crop_seconds','confirmation_id','policy')}
    inserted = db.execute('''INSERT INTO voice_profile_samples
        (id,root_profile_id,owner_id,contributor_id,model,nonce,ciphertext,state,evidence,payload_sha256)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id''',
        (body.id,root['id'],root['owner_id'],actor['id'],body.model,nonce,encrypted,state,Jsonb(evidence),fingerprint)).fetchone()
    if not inserted:
        raise HTTPException(409, 'voice_learning_sample_conflict')
    audit(db, actor['id'], 'voice.sample.submit', body.id, {'state': state, 'audio_stored': False})
    return {'id': body.id, 'state': state, 'audio_stored': False}


def accepted_profiles(db):
    return db.execute('''SELECT s.id,s.owner_id,s.model,s.nonce,s.ciphertext,s.updated_at,p.name,
        s.root_profile_id FROM voice_profile_samples s JOIN voice_profiles v ON v.id=s.root_profile_id
        JOIN principals p ON p.id=s.owner_id JOIN auth_identities a ON a.principal_id=p.id
        WHERE s.state='accepted' AND p.revoked_at IS NULL
          AND s.model=CASE WHEN v.model LIKE '%%#crop10s' THEN v.model ELSE v.model||'#crop10s' END
        ORDER BY s.updated_at DESC,s.id LIMIT 501''').fetchall()


def register(app, session_dependency):
    @app.post('/v1/voice/samples', status_code=201)
    def add_sample(body: Sample, context: session_dependency):
        db, actor = context
        return submit(db, actor, body)

    @app.get('/v1/voice/samples')
    def list_samples(context: session_dependency):
        db, actor = context
        rows = db.execute('''SELECT id,root_profile_id,owner_id,contributor_id,model,state,created_at,
            evidence->>'origin' AS origin FROM voice_profile_samples
            WHERE owner_id=%s OR contributor_id=%s ORDER BY created_at DESC LIMIT 128''', (actor['id'],actor['id'])).fetchall()
        return {'items': rows, 'audio_stored': False}

    @app.post('/v1/voice/samples/{sample_id}/accept')
    def accept_sample(sample_id: UUID, context: session_dependency):
        db, actor = context
        row = db.execute("UPDATE voice_profile_samples SET state='accepted',updated_at=now() WHERE id=%s AND owner_id=%s AND state='pending' RETURNING id", (sample_id,actor['id'])).fetchone()
        if not row:
            raise HTTPException(404, 'voice_learning_sample_unavailable')
        audit(db, actor['id'], 'voice.sample.accept', sample_id)
        return {'accepted': True}

    @app.delete('/v1/voice/samples/{sample_id}')
    def reject_sample(sample_id: UUID, context: session_dependency):
        db, actor = context
        row = db.execute('''UPDATE voice_profile_samples SET state='rejected',nonce=NULL,ciphertext=NULL,updated_at=now()
            WHERE id=%s AND (owner_id=%s OR contributor_id=%s) RETURNING id''', (sample_id,actor['id'],actor['id'])).fetchone()
        if not row:
            raise HTTPException(404, 'voice_learning_sample_unavailable')
        audit(db, actor['id'], 'voice.sample.revoke', sample_id)
        return {'deleted': True}
