#!/usr/bin/env python3
"""Replay frozen public model outputs through the product reference policy.

Chronology is synthetic; clean crops are oracle inputs. No new embedding inference
or employee profiles are used. Existing PLDA projection is executed locally.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--resident-cache', type=Path, required=True)
    parser.add_argument('--bridge-cache', type=Path, required=True)
    parser.add_argument('--reference-report', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--human-first-anchor', action='store_true')
    args = parser.parse_args()
    from synth.speakers import native
    from synth.speakers.profiles import normalized
    from synth.speakers.reference_session import ReferenceSession
    from synth.worker.desktop_pipeline import inference_lock, STATE
    manifest = json.loads((args.baseline/'manifest.json').read_text())
    cache = json.loads((args.resident_cache/'resident-vectors.json').read_text())
    ten = json.loads((args.bridge_cache/'vectors10.json').read_text())
    expected = json.loads(args.reference_report.read_text())
    installed = Path('/Applications/Synth.app/Contents/Resources/synth-host/_internal')
    native.LOCK = installed/'synth/resources/speaker-model-lock.json'
    models = installed/'synth/.runtime/models/speakers-community1'
    if (cache['signature']['manifest'] != sha(args.baseline/'manifest.json')
            or cache['signature']['binary'] != sha(args.binary)
            or cache['signature']['model_lock'] != sha(native.LOCK)):
        raise ValueError('reference_session_baseline_changed')
    model = json.loads(native.LOCK.read_text())['model_revision']
    profiles = []
    for identity in sorted({c['identity'] for c in manifest['cases'] if c['kind'] == 'enrollment'}):
        values = [cache['values'][c['id']]['vector'] for c in manifest['cases'] if c['kind'] == 'enrollment' and c['identity'] == identity]
        profiles.append({'id':'enrolled:'+identity, 'employee_id':identity, 'name':identity, 'model':model,
            'vector': normalized([sum(v[i] for v in values)/len(values) for i in range(256)])})
    probes = [c for c in manifest['cases'] if c['kind'] == 'probe']
    with inference_lock(STATE) as lease:
        projected = native.project_embeddings([p['vector'] for p in profiles]+[cache['values'][c['id']]['vector'] for c in probes],
            args.bridge_cache, binary=args.binary, models=models, lock_fd=lease)
    profiles = [{**p, 'vector':v} for p,v in zip(profiles, projected)]
    rho = {c['id']:v for c,v in zip(probes, projected[len(profiles):])}
    capture, owner = str(uuid4()), str(uuid4())
    groups = []
    for block in range(3):
        for identity in [p['identity'] for p in manifest['provenance'] if p['role'] != 'absent_profile']:
            cases = sorted([c for c in probes if c['block'] == block and c['identity'] == identity], key=lambda c:c['window'])
            start = len(groups)*160000
            common = {'capture_id':capture,'owner':owner,'source':'system','model_lock_sha256':sha(native.LOCK),
                      'policy':'guarded_disjoint_voice_windows_v1'}
            observations = [{**common,'id':c['id'],'crop_seconds':5,'model':model,'rho128':rho[c['id']],
                             'ranges':[[start+i*80000,start+(i+1)*80000]]} for i,c in enumerate(cases)]
            vector10 = ten[identity+':'+str(block)]['vector']
            if vector10 is not None:
                observations.append({**common,'id':identity+':'+str(block),'crop_seconds':10,
                    'model':model+'#crop10s','embedding':vector10,'ranges':[r for o in observations for r in o['ranges']]})
            groups.append((identity,block,cases[0]['role']=='known',observations))
    session = ReferenceSession(profiles, model)
    rows = []
    for identity,block,known,observations in groups:
        result = session.observe(identity+':'+str(block), observations)
        if args.human_first_anchor and known and block == 0:
            result = session.confirm(identity+':'+str(block), observations, 'enrolled:'+identity, str(uuid4()))
        rows.append({'identity':identity,'block':block,'known':known,'candidate':result['candidate_employee_id'],
                     'confirmed':result['confirmed']})
        if result['state'] == 'suggested':
            assert result['profile_id'] == 'enrolled:'+result['candidate_employee_id'] and not result['confirmed']
    if not args.human_first_anchor:
        assert [r['candidate'] for r in rows] == [r['candidate'] for r in expected['blocks']]
    revocations = []
    for revoked in [p['identity'] for p in manifest['provenance'] if p['role'] == 'known']:
        session = ReferenceSession([p for p in profiles if p['employee_id'] != revoked],model)
        outcomes = []
        for identity,block,known,observations in groups:
            result = session.observe(identity+':'+str(block),observations)
            if args.human_first_anchor and known and block == 0 and identity != revoked:
                result = session.confirm(identity+':'+str(block),observations,'enrolled:'+identity,str(uuid4()))
            outcomes.append(result)
        assert all(r['candidate_employee_id'] != revoked for r in outcomes)
        revocations.append({'public_identity':revoked,'name_absent':True})
    summary = {'known_correct':sum(r['known'] and r['candidate']==r['identity'] for r in rows),
        'known_blocks':sum(r['known'] for r in rows),
        'wrong_suggestions':sum(r['candidate'] is not None and (not r['known'] or r['candidate']!=r['identity']) for r in rows),
        'unknown_rejected':sum(not r['known'] and r['candidate'] is None for r in rows),
        'unknown_blocks':sum(not r['known'] for r in rows)}
    report = {'verification_tier':'real-model-cache-replay',
        'scope':'simulated_human_anchors' if args.human_first_anchor else 'product_reference_policy_parity',
        'same_decisions':None if args.human_first_anchor else len(rows),'summary':summary,'revocations':revocations,
        'simulated_human_confirmations':sum(r['confirmed'] for r in rows), 'real_human_validation':False,
        'baseline_without_human':expected['summary']['candidate'],
        'oracle_clean_crops':True,'synthetic_chronology':True,'new_embedding_calls':0,'new_projection_commands':1,
        'api_requests':0,'model_downloads':0,'local_cost_usd':None,'energy_wh':None,
        'inputs_sha256':{'manifest':sha(args.baseline/'manifest.json'),'five_second_cache':sha(args.resident_cache/'resident-vectors.json'),
            'ten_second_cache':sha(args.bridge_cache/'vectors10.json'),'reference_report':sha(args.reference_report)},
        'source_sha256':{name:sha(ROOT/name) for name in ('synth/speakers/reference_session.py',
            'synth/speakers/matching_legacy.py','synth/speakers/matching.py','synth/speakers/thresholds.json')}}
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if not k.endswith('sha256')}))


if __name__ == '__main__':
    main()
