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
from .onboarding import vault, samples, clean_samples, embed_chunk

PILOT_THRESHOLD=.65
PILOT_MARGIN=.10

def profile_signature(actor):
    from .sync import catalog
    profiles, signature, _ = catalog(actor)
    return signature, len(profiles)

def classify(vectors, profiles):
    if not vectors: return {"state":"unknown","reason":"short_or_unclean","similarity":None}
    mean=normalized([sum(v[i] for v in vectors)/len(vectors) for i in range(256)])
    by_employee={}
    for p in profiles:
        score=sum(a*b for a,b in zip(mean,p["vector"]))
        employee=p.get("employee_id",p["id"])
        if employee not in by_employee or score>by_employee[employee][0]: by_employee[employee]=(score,p)
    ranked=sorted(by_employee.values(),key=lambda x:-x[0])
    if not ranked: return {"state":"unknown","reason":"no_active_profiles","similarity":None}
    score,p=ranked[0]; margin=score-(ranked[1][0] if len(ranked)>1 else -1)
    votes=sum(sum(a*b for a,b in zip(v,p["vector"]))>=PILOT_THRESHOLD for v in vectors)
    reason='pilot_candidate' if score>=PILOT_THRESHOLD and margin>=PILOT_MARGIN and votes>=2 else 'below_threshold_or_unstable'
    return {"state":"candidate" if reason=='pilot_candidate' else "unknown","reason":reason,"similarity":round(score,4),"profile_id":p["id"] if reason=='pilot_candidate' else None,"employee_id":None,"candidate_employee_id":p.get("employee_id") if reason=='pilot_candidate' else None,"label":p.get("name","Tu voz") if reason=='pilot_candidate' else None,"matching_mode":"pilot_uncalibrated"}

def identify(actor,normalized_sources,diarizations,raw_root,lock_fd=None):
    from synth.worker.journal import file_hash
    from .native import LOCK
    from .sync import catalog
    profiles, signature, sync_state = catalog(actor)
    count = len(profiles)
    result={"state":"pilot_uncalibrated","profile_signature":signature,"sync_state":sync_state,"production_calibration":"pending","matches":{},"active_profiles":count}
    if not count: return {**result,"state":"unavailable","reason":"no_active_profiles"}
    model=json.loads(LOCK.read_text())["model_revision"]
    profiles = [p for p in profiles if p["model"] == model]
    result['active_profiles'] = len(profiles)
    if not profiles:
        return {**result, 'state': 'unavailable', 'reason': 'no_model_compatible_profiles'}
    raw_root=Path(raw_root).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='identity-',dir=raw_root) as temporary:
        directory=Path(temporary)
        for source,desc in normalized_sources.items():
            path=Path(desc['path']).resolve(strict=True)
            if not path.is_relative_to(raw_root) or file_hash(path)!=desc['sha256']: raise ValueError('identity_audio_changed')
            turns=diarizations[source]['segments']
            if not turns: continue
            audio=samples(path)
            for speaker in sorted({t['speaker_id'] for t in turns}):
                key=source+':'+speaker
                selected=[t for t in turns if t['speaker_id']==speaker]
                overlap=any(a['speaker_id']!=b['speaker_id'] and max(a['start_ms'],b['start_ms'])<min(a['end_ms'],b['end_ms']) for a in selected for b in turns)
                if overlap:
                    result['matches'][key]={"state":"unknown","reason":"overlap","similarity":None}; continue
                clean=clean_samples(audio,turns,speaker); length=min(80000,len(clean)); vectors=[]
                # A short sample cannot obtain multiple independent votes via duplicate crops.
                if len(clean)>=160000:
                    offsets=[0,(len(clean)-length)//2,len(clean)-length]
                    for i,offset in enumerate(offsets): vectors.append(embed_chunk(clean[offset:offset+length],directory,i,lock_fd))
                outcome=classify(vectors,profiles)
                outcome['clean_seconds']=round(len(clean)/16000,3)
                result['matches'][key]=outcome
    return result


def analyze_existing(actor,task):
    from synth.worker.desktop_pipeline import STATE,CAPTURE,restore_json,save_json,inference_lock
    output=STATE/'tasks'/task['id']; raw=CAPTURE/('processed-'+task['id']); cp=task['checkpoints']
    with inference_lock(STATE) as fd:
        ds=restore_json(cp['diarization'],output)
        result=identify(actor,cp['normalized'],ds,raw,fd)
        from .meeting_status import transcript_hash
        canonical=restore_json(cp['canonical'],output)
        result.update(processing_mode='automatic', transcript_hash=transcript_hash(canonical))
        save_json(output/'identity-status.json',result)
    import sqlite3
    with sqlite3.connect(STATE/'queue.sqlite3') as db:
        db.execute("UPDATE desktop_tasks SET result=json_set(result,'$.identity',json(?)) WHERE id=? AND state='succeeded'",(json.dumps(result),task['id']))
    # A deleted/replaced profile invalidates this cache immediately at read time.
    return result
