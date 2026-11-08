"""Meeting/profile comparison kept on this Mac; pilot candidates are not identities.
A fixed conservative pilot rule is distinct from the held-out production gate.
Never train or enroll on meeting audio, export embeddings, or infer from a channel.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import tempfile
from .profiles import normalized
from .onboarding import vault, samples, embed_chunk
from .windows import voice_windows, configured_space, analysis_policy

PILOT_THRESHOLD=.90
PILOT_MARGIN=.10

def profile_signature(actor):
    from .sync import catalog
    profiles, signature, _ = catalog(actor)
    return signature, len(profiles)

def classify(vectors, profiles, dimensions=256):
    if not vectors: return {"state":"unknown","reason":"short_or_unclean","similarity":None}
    vectors = [normalized(vector,dimensions) for vector in vectors]
    try:
        mean=normalized([sum(v[i] for v in vectors)/len(vectors) for i in range(dimensions)],dimensions)
    except ValueError as error:
        if str(error) != 'empty_embedding':raise
        return {'state':'unknown','reason':'inconsistent_voice_windows','similarity':None}
    by_employee={}
    for p in profiles:
        score=sum(a*b for a,b in zip(mean,p["vector"]))
        employee=p.get("employee_id",p["id"])
        if employee not in by_employee or score>by_employee[employee][0]: by_employee[employee]=(score,p)
    ranked=sorted(by_employee.values(),key=lambda x:-x[0])
    if not ranked: return {"state":"unknown","reason":"no_active_profiles","similarity":None}
    score,p=ranked[0]; margin=score-(ranked[1][0] if len(ranked)>1 else -1)
    # Each independent window must prefer the same employee over alternatives.
    # A centroid between two enrolled voices must not invent a third identity.
    employee = p.get('employee_id',p['id'])
    window_scores, window_margins = [], []
    for vector in vectors:
        scores = {}
        for profile in profiles:
            identifier = profile.get('employee_id',profile['id'])
            similarity = sum(a*b for a,b in zip(vector,profile['vector']))
            scores[identifier] = max(scores.get(identifier,-1), similarity)
        similarity = scores[employee]
        window_scores.append(similarity)
        window_margins.append(similarity-max((value for key,value in scores.items() if key != employee),default=-1))
    votes=sum(similarity>=PILOT_THRESHOLD and margin>=PILOT_MARGIN for similarity,margin in zip(window_scores,window_margins))
    reason='pilot_candidate' if score>=PILOT_THRESHOLD and margin>=PILOT_MARGIN and votes>=2 else 'below_threshold_or_unstable'
    return {"state":"candidate" if reason=='pilot_candidate' else "unknown","reason":reason,"similarity":round(score,4),"profile_id":p["id"] if reason=='pilot_candidate' else None,"employee_id":None,"candidate_employee_id":p.get("employee_id") if reason=='pilot_candidate' else None,"label":p.get("name","Tu voz") if reason=='pilot_candidate' else None,"matching_mode":"pilot_uncalibrated",
            "supporting_windows":votes,"window_similarities":[round(value,4) for value in window_scores],
            "window_margins":[round(value,4) for value in window_margins],"margin":round(margin,4)}

def identify(actor,normalized_sources,diarizations,raw_root,lock_fd=None,embedding_space=None,
             catalog_snapshot=None, task_id=None):
    from synth.worker.journal import file_hash
    from .native import LOCK
    from .sync import catalog
    profiles, signature, sync_state = catalog_snapshot if catalog_snapshot is not None else catalog(actor)
    space = embedding_space or configured_space()
    count = len(profiles)
    result={"state":"pilot_uncalibrated","profile_signature":signature,"sync_state":sync_state,"production_calibration":"pending","matches":{},"active_profiles":count,
            "analysis_policy":analysis_policy(space), "embedding_space":space,
            "pilot_threshold":PILOT_THRESHOLD, "pilot_margin":PILOT_MARGIN}
    if not count: return {**result,"state":"unavailable","reason":"no_active_profiles"}
    model=json.loads(LOCK.read_text())["model_revision"]
    profiles = [p for p in profiles if p["model"] == model]
    result['active_profiles'] = len(profiles)
    if not profiles:
        return {**result, 'state': 'unavailable', 'reason': 'no_model_compatible_profiles'}
    raw_root=Path(raw_root).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='identity-',dir=raw_root) as temporary:
        directory=Path(temporary)
        if space == 'plda_rho128_v1':
            from .native import project_embeddings
            projected = project_embeddings([p['vector'] for p in profiles],directory,lock_fd=lock_fd)
            profiles = [{**p,'vector':vector} for p,vector in zip(profiles,projected)]
        elif space != 'cosine256':
            raise ValueError('speaker_embedding_space_invalid')
        for source,desc in normalized_sources.items():
            path=Path(desc['path']).resolve(strict=True)
            if not path.is_relative_to(raw_root) or file_hash(path)!=desc['sha256']: raise ValueError('identity_audio_changed')
            turns=diarizations[source]['segments']
            if not turns: continue
            audio=samples(path)
            for speaker in sorted({t['speaker_id'] for t in turns}):
                key=source+':'+speaker
                windows, selection = voice_windows(audio, turns, speaker)
                vectors, failures, evidence = [], [], []
                for i, (chunk, descriptor) in enumerate(windows):
                    try:
                        vectors.append(embed_chunk(chunk,directory,i,lock_fd))
                        evidence.append({**descriptor, 'state': 'valid'})
                    except (ValueError, OSError) as error:
                        code = str(error)
                        if not (code.startswith(('speaker_', 'voice_')) and code.replace('_', '').isalnum() and len(code) < 100):
                            code = type(error).__name__
                        failures.append(code)
                        evidence.append({**descriptor, 'state': 'rejected', 'error_code': code})
                if task_id and vectors:
                    from .prints import open_prints
                    store = open_prints()
                    try:
                        store.store_windows(task_id, key, model, vectors, selection['clean_seconds'],
                                            analysis_policy(space), evidence)
                    finally:
                        store.close()
                if failures and len(vectors) < 2:
                    result['matches'][key] = {'state': 'unknown', 'reason': 'voice_embedding_failed',
                                              'cause': failures[0], 'similarity': None,
                                              'valid_windows': len(vectors), 'rejected_windows': len(failures),
                                              **selection, 'window_evidence': evidence}
                    continue
                if vectors and space == 'plda_rho128_v1':
                    try:
                        vectors=project_embeddings(vectors,directory,lock_fd=lock_fd)
                    except (ValueError,OSError):
                        result['matches'][key]={'state':'unknown','reason':'voice_projection_failed','similarity':None,
                                                **selection,'window_evidence':evidence}
                        continue
                outcome=classify(vectors,profiles,dimensions=128 if space == 'plda_rho128_v1' else 256)
                if not windows and selection['overlap_seconds'] > 0:
                    outcome['reason'] = 'overlap'
                outcome.update(selection, valid_windows=len(vectors), rejected_windows=len(failures),
                               window_evidence=evidence)
                if failures:
                    outcome['embedding_failures']=sorted(set(failures))
                result['matches'][key]=outcome
    return result


def analyze_existing(actor,task):
    from synth.worker.desktop_pipeline import STATE,CAPTURE,restore_json,save_json,inference_lock
    output=STATE/'tasks'/task['id']; raw=CAPTURE/('processed-'+task['id']); cp=task['checkpoints']
    with inference_lock(STATE) as fd:
        ds=restore_json(cp['diarization'],output)
        sources=restore_json(cp['acoustic'],output)['sources'] if 'acoustic' in cp else cp['normalized']
        result=identify(actor,sources,ds,raw,fd)
        from .meeting_status import transcript_hash
        canonical=restore_json(cp['canonical'],output)
        result.update(processing_mode='automatic', transcript_hash=transcript_hash(canonical))
        save_json(output/'identity-status.json',result)
    import sqlite3
    with sqlite3.connect(STATE/'queue.sqlite3') as db:
        db.execute("UPDATE desktop_tasks SET result=json_set(result,'$.identity',json(?)) WHERE id=? AND state='succeeded'",(json.dumps(result),task['id']))
    # A deleted/replaced profile invalidates this cache immediately at read time.
    return result
