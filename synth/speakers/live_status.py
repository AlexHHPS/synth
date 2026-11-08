"""Owner-fenced live suggestions from ciphertext and the current profile catalog.

No raw vector is returned. No worker/model inference is started by a browser;
the only computation here is local PLDA projection and comparison of observations.
"""
import hashlib
import json
from pathlib import Path

from synth.worker.live_voice import POLICY, load_owner
from synth.worker.voice_binding import parse
from . import native
from .observations import load_observation
from .reference_session import ReferenceSession, pair
from .matching import suggest
from .capture_decisions import CaptureDecisions, evidence_hash


def capture_status(capture_id, identity, actor, state, capture_root, *, snapshot=None, key=None,
                   project=None, model_lock=None, _evidence=None, _seal=False, _learn=True):
    from uuid import UUID
    capture_id = str(UUID(capture_id))
    location = Path(state)/'incremental'/capture_id/'voice-capture.json'
    if not location.exists():
        return {'capture_id': capture_id, 'state': 'collecting', 'matches': [], 'confirmed': False}
    if location != location.resolve(strict=True) or location.stat().st_size > 8192:
        raise ValueError('voice_capture_path_invalid')
    value = json.loads(location.read_text())
    if value['capture_id'] != capture_id or value['owner'] != identity or value['policy'] != POLICY:
        raise ValueError('voice_capture_owner_mismatch')
    folder = Path(value['folder'])
    root = Path(capture_root).resolve(strict=True)
    if folder != folder.resolve(strict=True) or not folder.is_relative_to(root):
        raise ValueError('voice_capture_path_invalid')
    if load_owner(folder, capture_id) != identity:
        raise ValueError('voice_capture_owner_mismatch')
    if snapshot is None:
        from .sync import catalog
        snapshot = catalog(actor)  # never reuse a coworker name after revoked access
    profiles, signature, sync_state = snapshot
    if key is None:
        from .keychain import obtain
        key = obtain(Path(state).parent/'voice/vault-key')
    lock_path = Path(model_lock or native.LOCK)
    model = json.loads(lock_path.read_text())['model_revision']
    legacy = [p for p in profiles if p['model'] == model]
    crop10 = [p for p in profiles if p['model'] == model+'#crop10s']
    if legacy:
        from tempfile import TemporaryDirectory
        # PLDA only, no second diarizer or Qwen. The existing adapter validates
        # pinned models and removes its private intermediate files on completion.
        with TemporaryDirectory(prefix='voice-project-', dir=folder) as temporary:
            projected = (project or native.project_embeddings)([p['vector'] for p in legacy], Path(temporary))
        legacy = [{**p, 'vector': v} for p,v in zip(legacy, projected)]
    session = ReferenceSession(legacy, model, identities=profiles)
    with CaptureDecisions(state, capture_id, identity) as store:
        decision_revision, decisions = store.seal() if _seal else store.load()
    directory = folder/'voice-context-v1'
    paths = list(directory.glob('*/window.json'))
    if len(paths) > 2880:
        raise ValueError('voice_capture_window_limit')
    records = []
    for path in paths:
        if path != path.resolve(strict=True) or path.stat().st_size > 2*1024*1024:
            raise ValueError('voice_capture_path_invalid')
        record = json.loads(path.read_text())
        meta = record['input']
        if (meta['capture_id'] != capture_id or meta['owner'] != identity or meta['policy'] != POLICY
                or type(meta['start_frame']) is not int or type(meta['end_frame']) is not int
                or not 0 <= meta['start_frame'] < meta['end_frame'] <= 86400*16000):
            raise ValueError('voice_capture_window_invalid')
        if record['state'] == 'complete' and meta['end_frame'] <= value['through_frame']:
            records.append(record)
    matches, contexts = [], []
    for record in sorted(records, key=lambda r: r['input']['start_frame']):
        meta = record['input']
        for source, result in sorted(record['sources'].items()):
            if source not in ('microphone', 'system'):
                raise ValueError('voice_capture_window_invalid')
            for group in result['groups']:
                observations = []
                for observation in group['observations']:
                    aad = parse(observation['binding'])
                    if (aad['capture_id'] != capture_id or aad['owner'] != identity or aad['source'] != source
                            or any(a < meta['start_frame'] or b > meta['end_frame'] for a,b in aad['ranges'])
                            or hashlib.sha256(observation['binding'].encode()).hexdigest() != observation['id']):
                        raise ValueError('voice_capture_observation_invalid')
                    observations.append({**load_observation(observation['descriptor'], observation['binding'],
                        directory, key, model_lock=lock_path), 'id': observation['id']})
                five, ten = pair(observations, model)
                direct10 = suggest(ten['embedding'] if ten else None, 10 if ten else 0, crop10)
                competing = direct10.get('candidate_employee_id') if direct10['state'] in ('suggested','ask') else None
                decision = decisions.get(group['id'])
                if decision is None:
                    session.observe_crop10(group['id'],observations,crop10)
                outcome = session.observe(group['id'], observations, competing_candidate=competing)
                proposals = [r for r in (outcome, direct10) if r['state'] in ('suggested', 'ask')]
                if len({r['candidate_employee_id'] for r in proposals}) > 1:
                    outcome = {**outcome, 'state': 'unknown', 'reason': 'crop_policy_disagreement',
                        'candidate_employee_id': None, 'candidate_name': None, 'profile_id': None}
                elif (outcome['state'] == 'unknown' and outcome['reason'] != 'crop_policy_disagreement'
                      and direct10['state'] in ('suggested', 'ask')):
                    outcome = {**outcome, **direct10, 'reason': 'enrolled_crop10_profile'}
                if (ten is not None and decision is not None
                        and decision['profile_id'] in session.identities
                        and evidence_hash(outcome['evidence_ids']) == decision['evidence_hash']):
                    outcome = session.confirm(group['id'], observations, decision['profile_id'], decision['decision_id'])
                context = {'group_id': group['id'], 'observations': observations, 'folder': folder,
                           'can_identify': ten is not None, 'evidence_ids': outcome['evidence_ids']}
                contexts.append(context)
                matches.append({**outcome, 'source': source, 'start_ms': meta['start_frame']//16,
                    'can_identify': ten is not None,
                    'end_ms': meta['end_frame']//16,
                    'speech_spans_ms': [[meta['start_frame']/16+s['start_ms'], meta['start_frame']/16+s['end_ms']]
                                        for s in result['segments'] if s['speaker_id'] == group['speaker_id']],
                    'source_ranges_ms': [[a/16,b/16] for o in five for a,b in o['ranges']]})
    # A later strong or human anchor can clarify earlier unknown blocks. These
    # remain suggestions; a second pass cannot train a reference from fallback.
    for index, context in enumerate(contexts):
        if matches[index]['state'] == 'unknown' and matches[index]['reason'] != 'crop_policy_disagreement':
            revised = session.observe(context['group_id'], context['observations'])
            if revised['state'] == 'suggested':
                matches[index].update(revised)
    if _evidence is not None:
        _evidence.extend(contexts)
    try:
        from .learning import collect
        learning = collect(actor, contexts, matches, profiles, legacy, model) if _learn else {'enabled':False}
    except Exception:
        # Voice learning cannot interrupt recording or conceal suggestions.
        learning = {'enabled':True,'state':'unavailable'}
    return {'capture_id': capture_id, 'state': value['state'], 'error_code': value.get('error_code'),
        'learning':learning,
        'confirmation_revision': decision_revision,
        'eligible_profiles': [{k: p.get(k) for k in ('id', 'name', 'employee_id','root_profile_id')} for p in profiles
                              if p['id'] in session.identities],
        'through_ms': value['through_frame']//16, 'profile_signature': signature, 'sync_state': sync_state,
        'active_profiles': len(legacy)+len(crop10), 'matches': matches, 'confirmed': any(m['confirmed'] for m in matches),
        'embedding_space': 'plda_rho128_v1',
        'matching_mode': 'session_reference_suggestions', 'production_calibration': 'pending'}


def confirm_capture(capture_id, identity, actor, state, capture_root, body, *, guard=lambda: None, **dependencies):
    contexts = []
    current = capture_status(capture_id, identity, actor, state, capture_root, _evidence=contexts, _learn=False, **dependencies)
    if not isinstance(body, dict):
        raise ValueError('voice_confirmation_invalid')
    context = next((c for c in contexts if c['group_id'] == body.get('group_id')), None)
    if context is None or not context['can_identify']:
        raise ValueError('voice_confirmation_evidence_unavailable')
    if evidence_hash(body.get('evidence_ids')) != evidence_hash(context['evidence_ids']):
        raise ValueError('voice_confirmation_evidence_changed')
    if body.get('action') == 'confirm' and body.get('profile_id') not in {p['id'] for p in current['eligible_profiles']}:
        raise ValueError('voice_confirmation_profile_unavailable')
    with CaptureDecisions(state, capture_id, identity) as store:
        store.apply(body, guard=guard)
    try:
        from .learning import retract_capture_group
        target = next((p['employee_id'] for p in current['eligible_profiles'] if p['id']==body.get('profile_id')),None)
        keep = [p.get('root_profile_id') or p['id'] for p in current['eligible_profiles'] if target and p['employee_id']==target]
        retract_capture_group(actor,capture_id,body['group_id'],keep)
    except Exception:
        # A retained encrypted retraction is retried when synchronization returns.
        pass
    return capture_status(capture_id, identity, actor, state, capture_root, **dependencies)
