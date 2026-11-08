#!/usr/bin/env python3
"""Real native live collector on public audio, without employees or audio devices.

This proves prepared-slab observation, encrypted host matching and restart reuse.
It does not run Qwen, validate its text, or claim final acta/UI acceptance.
"""
import argparse
from array import array
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from uuid import uuid4
import wave

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--resident-cache', type=Path, required=True)
    parser.add_argument('--bridge-cache', type=Path, required=True)
    parser.add_argument('--private', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--embedding-baseline-binary', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    import numpy as np
    from synth.worker.live_voice import LiveVoiceObserver, verify_assets
    from synth.worker.desktop_pipeline import inference_lock, STATE
    from synth.speakers.live_status import capture_status
    from synth.speakers.profiles import normalized
    from synth.speakers import native
    manifest = json.loads((args.baseline/'manifest.json').read_text())
    cache = json.loads((args.resident_cache/'resident-vectors.json').read_text())
    ten_cache = json.loads((args.bridge_cache/'vectors10.json').read_text())
    installed = Path('/Applications/Synth.app/Contents/Resources/synth-host/_internal')
    lock = installed/'synth/resources/speaker-model-lock.json'
    models = installed/'synth/.runtime/models/speakers-community1'
    if (cache['signature']['manifest'] != sha(args.baseline/'manifest.json')
            or cache['signature']['binary'] != sha(args.embedding_baseline_binary or args.binary)
            or cache['signature']['model_lock'] != sha(lock)):
        raise ValueError('live_voice_baseline_changed')
    model = json.loads(lock.read_text())['model_revision']
    selected = {role: next(p['identity'] for p in manifest['provenance'] if p['role'] == role)
                for role in ('known', 'unknown')}
    sources = {'system': selected['known'], 'microphone': selected['unknown']}
    audio = {}
    for source, identity in sources.items():
        pcm = array('h')
        for block in range(3):
            path = args.bridge_cache/(identity+'-'+str(block)+'.wav')
            if sha(path) != ten_cache[identity+':'+str(block)]['input_sha256']:
                raise ValueError('live_voice_public_input_changed')
            with wave.open(str(path)) as stream:
                assert (stream.getframerate(),stream.getnchannels(),stream.getsampwidth(),stream.getnframes()) == (16000,1,2,160000)
                pcm.frombytes(stream.readframes(stream.getnframes()))
        audio[source] = np.asarray(pcm, dtype=np.float32)/32768
    profiles = []
    for identity in sorted({c['identity'] for c in manifest['cases'] if c['kind'] == 'enrollment'}):
        vectors = [cache['values'][c['id']]['vector'] for c in manifest['cases'] if c['kind'] == 'enrollment' and c['identity'] == identity]
        profiles.append({'id': identity, 'employee_id': identity, 'name': identity, 'model': model,
                         'vector': normalized([sum(v[i] for v in vectors)/len(vectors) for i in range(256)])})
    args.private.mkdir(parents=True, mode=0o700, exist_ok=True)
    run = args.private/str(uuid4())
    folder = run/'capture/public-replay'; folder.mkdir(parents=True, mode=0o700)
    state = run/'pipeline'; owner, capture = str(uuid4()), str(uuid4())
    (folder/'capture-owner.json').write_text(json.dumps({'version':1,'capture_id':capture,'identity':owner}))
    config = {'binary': str(args.binary), 'models': str(models), 'lock': str(lock)}
    key = os.urandom(32)  # not written to files or output
    started = time.monotonic()
    with inference_lock(STATE) as lease:
        observer = LiveVoiceObserver(folder, capture, state, config, lease, key_provider=lambda:key)
        try:
            for start in range(0,30,3):
                observer({s:a[start*16000:(start+3)*16000] for s,a in audio.items()},start*48000,(start+3)*48000)
            observer.finish()
            assert observer.state == 'drained', observer.error_code
            first_counts = {'diarization':observer.diarization_calls,'embedding':observer.embedding_calls}
            first_load = observer.worker.load_seconds
        finally:
            observer.close()
        replay = LiveVoiceObserver(folder,capture,state,config,lease,key_provider=lambda:key)
        try:
            for start in range(0,30,3):
                replay({s:a[start*16000:(start+3)*16000] for s,a in audio.items()},start*48000,(start+3)*48000)
            replay.finish()
            assert replay.state == 'drained' and replay.reused_windows == 1
            assert replay.diarization_calls == replay.embedding_calls == 0
        finally:
            replay.close()
    native.LOCK = lock
    def project(vectors, directory):
        return native.project_embeddings(vectors,directory,binary=args.binary,models=models)
    result = capture_status(capture,owner,{'id':owner},state,run/'capture',snapshot=(profiles,'frozen-public','local_only'),
        key=key,project=project,model_lock=lock)
    revoked = capture_status(capture,owner,{'id':owner},state,run/'capture',snapshot=([],'revoked-public','local_only'),
        key=key,project=project,model_lock=lock)
    assert all(r.get('candidate_employee_id') is None for r in revoked['matches'])
    assert not any(r['confirmed'] or r.get('employee_id') for r in result['matches'])
    assert '"embedding":' not in json.dumps(result) and '"rho128":' not in json.dumps(result)
    assert not list((folder/'voice-context-v1').rglob('*.wav'))
    wrong = [r for r in result['matches'] if r.get('candidate_employee_id') is not None
             and r['candidate_employee_id'] != sources[r['source']]]
    files = [ROOT/name for name in ('synth/worker/live_voice.py','synth/worker/speaker_session.py',
        'synth/worker/voice_windows.py','synth/worker/voice_binding.py','synth/worker/voice_key.py',
        'synth/speakers/live_status.py','synth/speakers/reference_session.py','synth/speakers/observations.py')]
    report = {'verification_tier':'real-model','scope':'live_voice_collector_and_encrypted_host_matching',
        'public_audio_only':True,'user_profiles_used':False,'qwen_executed':False,'audio_seconds':30,'sources':2,
        'groups':len(result['matches']),'suggestions':sum(r['state']=='suggested' for r in result['matches']),
        'wrong_suggestions':len(wrong),'confirmed_identities':0,'revocation_names_cleared':True,
        'first_pass_model_operations':first_counts,'restart_new_embedding_calls':0,'restart_new_diarization_calls':0,
        'worker_loads':2,'first_helper_load_seconds':first_load,'api_requests':0,'model_downloads':0,
        'no_raw_vectors_in_status':True,'temporary_crops_deleted':True,'total_seconds':time.monotonic()-started,
        'model_lock_sha256':sha(lock),'binary_sha256':sha(args.binary),
        'enrollment_baseline_binary_sha256':sha(args.embedding_baseline_binary or args.binary),
        'source_sha256':{str(p.relative_to(ROOT)):sha(p) for p in files+[Path(__file__)]},
        'local_cost_usd':None,'energy_wh':None,
        'limitations':['Two simultaneous public assembled tracks; not a real device capture or employee calibration.',
            'This replay exercises the actual collector and helper; it does not execute Qwen, Hermes, the UI or generate minutes.',
            'Known and unknown source assignments are used only to score, never passed to recognition.',
            'Labels remain suggestions; no threshold or enrollment changes.']}
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('source_sha256','limitations')}))


if __name__ == '__main__':
    main()
