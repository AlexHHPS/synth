#!/usr/bin/env python3
"""Public frozen voice cases through one resident process, without enrollment.

Reuse old embeddings only after audio/manifest/installed-binary hash checks.
The output contains counts, errors and timings; public vectors stay in the
explicit private experiment directory. This is compatibility, not calibration.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import wave
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    temporary = path.with_suffix(path.suffix+'.part')
    with os.fdopen(os.open(temporary, os.O_CREAT|os.O_TRUNC|os.O_WRONLY, 0o600), 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--private', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--baseline-report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--installed-internal', type=Path, default=Path('/Applications/Synth.app/Contents/Resources/synth-host/_internal'))
    args = parser.parse_args()
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    from synth.speakers import native
    from synth.speakers.matching_legacy import classify
    from synth.speakers.profiles import normalized
    from synth.speakers.observations import load_observation
    from synth.worker.voice_binding import binding as voice_binding
    from synth.worker.desktop_pipeline import STATE, inference_lock

    manifest = json.loads((args.baseline/'manifest.json').read_text())
    baseline_signature = json.loads((args.baseline/'runtime-signature.json').read_text())
    baseline = json.loads((args.baseline/'private-vectors.json').read_text())
    previous = json.loads(args.baseline_report.read_text())
    old_binary = args.installed_internal/'synth/.runtime/swift-build/release/synth-speakers'
    models = args.installed_internal/'synth/.runtime/models/speakers-community1'
    native.LOCK = args.installed_internal/'synth/resources/speaker-model-lock.json'
    if (sha(args.baseline/'manifest.json') != baseline_signature['manifest']
            or sha(native.LOCK) != baseline_signature['lock'] or sha(old_binary) != baseline_signature['binary']
            or previous['manifest_sha256'] != baseline_signature['manifest']):
        raise ValueError('voice_baseline_changed')
    args.private.mkdir(parents=True, exist_ok=True, mode=0o700)
    work = args.private/'work'; work.mkdir(exist_ok=True, mode=0o700)
    signature = {'manifest': baseline_signature['manifest'], 'binary': sha(args.binary),
                 'model_lock': sha(native.LOCK), 'client': sha(ROOT/'synth/worker/speaker_session.py')}
    cache = args.private/'resident-vectors.json'
    saved = json.loads(cache.read_text()) if cache.exists() else {'signature': signature, 'values': {}}
    if saved['signature'] != signature:
        raise ValueError('resident_voice_runtime_changed')
    values = saved['values']
    paths = {}
    for case in manifest['cases']:
        path = args.baseline/case['audio']
        if sha(path) != case['sha256'] or baseline[case['id']]['vector'] is None:
            raise ValueError('voice_baseline_audio_changed')
        target = work/case['audio']
        if target.exists() and sha(target) != case['sha256']:
            raise ValueError('voice_probe_changed')
        if not target.exists():
            shutil.copyfile(path, target); target.chmod(0o600)
        paths[case['id']] = target

    runtime = None
    calls = 0
    executed_embedding_seconds = 0.
    began = time.monotonic()
    try:
        with inference_lock(STATE) as fd:
            runtime = native.resident_session(work, binary=args.binary, models=models, lock_fd=fd)
            for case in manifest['cases']:
                if case['id'] in values:
                    continue
                result = runtime.embed(paths[case['id']])
                calls += 1
                executed_embedding_seconds += result['elapsed_seconds']
                values[case['id']] = {'vector': result['embedding'], 'seconds': result['elapsed_seconds']}
                save(cache, saved)
                print(json.dumps({'completed': len(values), 'cases': len(manifest['cases'])}), flush=True)
            similarities = [sum(a*b for a, b in zip(values[c['id']]['vector'], baseline[c['id']]['vector']))
                            for c in manifest['cases']]
            if min(similarities) < .99999:
                raise ValueError('resident_embedding_changed')
            profiles = []
            for identity in sorted({c['identity'] for c in manifest['cases'] if c['kind'] == 'enrollment'}):
                selected = [values[c['id']]['vector'] for c in manifest['cases'] if c['kind'] == 'enrollment' and c['identity'] == identity]
                centroid = normalized([sum(v[i] for v in selected)/len(selected) for i in range(256)])
                profiles.append({'id': identity, 'employee_id': identity, 'vector': centroid})
            probes = [c for c in manifest['cases'] if c['kind'] == 'probe']
            inputs = [p['vector'] for p in profiles]+[values[c['id']]['vector'] for c in probes]
            projected = runtime.project(inputs)
            reference = native.project_embeddings(inputs, work, binary=old_binary, models=models, lock_fd=fd)
            projection_error = max(abs(a-b) for p, q in zip(projected, reference) for a, b in zip(p, q))
            if projection_error > .00001:
                raise ValueError('resident_projection_changed')
            projected_profiles = [{**p, 'vector': v} for p, v in zip(profiles, projected)]
            projected_probes = {c['id']: v for c, v in zip(probes, projected[len(profiles):])}
            outcomes = []
            for row in previous['blocks']:
                selected = [c for c in probes if c['identity'] == row['identity'] and c['block'] == row['block']]
                outcome = classify([projected_probes[c['id']] for c in selected], projected_profiles, 128)
                if outcome.get('candidate_employee_id') != row['baseline_candidate']:
                    raise ValueError('resident_profile_decision_changed')
                outcomes.append({'known': row['known'], 'correct': outcome.get('candidate_employee_id') == row['identity'],
                                 'candidate': outcome.get('candidate_employee_id') is not None})

            # Real cross-language encryption, including raw and projected spaces.
            case = manifest['cases'][0]
            key = os.urandom(32)
            binding = voice_binding('11111111-1111-4111-8111-111111111111',
                '22222222-2222-4222-8222-222222222222', 'system',
                json.loads(native.LOCK.read_text())['model_revision'], sha(native.LOCK), 5, [[0, 80000]], case['sha256'])
            output = work/('sealed-'+os.urandom(8).hex()+'.json')
            receipt = runtime.embed_sealed(paths[case['id']], key, binding, output)
            encrypted = json.loads(output.read_text())
            assert set(encrypted) == {'version', 'binding', 'combined'}
            combined = base64.b64decode(encrypted['combined'], validate=True)
            plaintext = AESGCM(key).decrypt(combined[:12], combined[12:], binding.encode())
            observation = json.loads(plaintext)
            assert observation['revision'] == native.REVISION and observation['duration_ms'] == 5000
            assert max(abs(a-b) for a, b in zip(observation['embedding'], values[case['id']]['vector'])) < .00001
            rho = runtime.project([values[case['id']]['vector']])[0]
            assert max(abs(a-b) for a, b in zip(observation['rho128'], rho)) < .00001
            try:
                AESGCM(key).decrypt(combined[:12], combined[12:], (binding+'another-capture').encode())
                raise AssertionError('Another capture decrypted the observation')
            except InvalidTag:
                pass
            assert output.stat().st_mode & 0o777 == 0o600
            assert 'embedding' not in receipt and 'rho128' not in receipt
            loaded = load_observation(receipt, binding, work, key)
            assert loaded['embedding'] == observation['embedding'] and loaded['rho128'] == observation['rho128']

            # A segmentation request after many embedding calls is independent.
            first = runtime.diarize(paths[case['id']])
            runtime.embed(paths[case['id']])
            second = runtime.diarize(paths[case['id']])
            assert first['segments'] == second['segments']
            assert first['speech_state'] == second['speech_state']
            assert first['time_policy'] == 'intersection_with_physical_audio_v1'
            assert max(s['end_ms'] for s in first['raw_segments']) > 5000
            assert all(0 <= s['start_ms'] < s['end_ms'] <= 5000 for s in first['segments'])
            try:
                native.infer('diarize', paths[case['id']], work, binary=old_binary, models=models, lock_fd=fd)
                raise AssertionError('Frozen short-clip baseline no longer reproduces')
            except ValueError as error:
                assert str(error) == 'speaker_segment_invalid'
            corrected_cli = native.infer('diarize', paths[case['id']], work, binary=args.binary, models=models, lock_fd=fd)
            assert corrected_cli['segments'] == first['segments']
        # The resident child, not the now-closed parent fd, still owns the lease.
        try:
            with inference_lock(STATE):
                raise AssertionError('Child did not retain acoustic lease')
        except ValueError as error:
            assert str(error) == 'acoustic_worker_already_running'
        runtime.close()
        assert runtime.process.poll() == 0
        # Exercise the same lazy adapter scope used by final profile matching.
        # Only asset locations are redirected to this isolated build; inference
        # and projection are real, under the actual acoustic lease.
        factory = native.resident_session
        def isolated_factory(directory, **kwargs):
            return factory(directory, binary=args.binary, models=models, lock_fd=kwargs['lock_fd'])
        with inference_lock(STATE) as fd, patch.object(native, 'resident_session', side_effect=isolated_factory) as created:
            with native.reuse_models(work, lock_fd=fd) as scope:
                embedded = native.infer('embed', paths[case['id']], work, lock_fd=fd)
                projected_scope = native.project_embeddings([embedded['embedding']], work, lock_fd=fd)
                assert max(abs(a-b) for a, b in zip(embedded['embedding'], values[case['id']]['vector'])) < .00001
                assert max(abs(a-b) for a, b in zip(projected_scope[0], rho)) < .00001
                assert created.call_count == 1
            assert scope['worker'].closed and scope['worker'].process.poll() == 0
        report = {'status': 'PASS', 'verification_tier': 'real-model',
            'scope': 'resident_voice_model_compatibility_and_sealed_observations',
            'user_profiles_used': False, 'user_audio_used': False, 'api_requests': 0, 'model_downloads': 0,
            'signature': signature, 'baseline': baseline_signature, 'cases': len(manifest['cases']),
            'new_embedding_calls_in_case_loop': calls, 'helper_model_loads': 2,
            'model_operations_this_run': {'unsealed_embedding': calls+2, 'sealed_embedding': 1,
                'projection_commands': 4, 'projection_inside_seal': 1, 'diarization_commands': 4,
                'cached_case_embeddings': len(values)-calls, 'one_shot_helper_invocations': 3},
            'raw_vector_cosine_min': min(similarities), 'projected_max_abs_difference': projection_error,
            'same_legacy_decisions': len(outcomes), 'known_correct': sum(r['known'] and r['correct'] for r in outcomes),
            'known_blocks': sum(r['known'] for r in outcomes), 'wrong_suggestions': sum(r['candidate'] and not r['correct'] for r in outcomes),
            'unknown_rejected': sum(not r['known'] and not r['candidate'] for r in outcomes),
            'unknown_blocks': sum(not r['known'] for r in outcomes),
            'load_seconds': runtime.load_seconds, 'total_seconds': time.monotonic()-began,
            'case_embedding_seconds': sum(v['seconds'] for v in values.values()),
            'case_embedding_seconds_executed_this_run': executed_embedding_seconds,
            'case_timing_scope': 'native request bodies; excludes model load and IPC; timings may be reused from identical runtime cache',
            'timings_comparable_for_speedup': False,
            'baseline_embedding_seconds_including_old_process_loads': sum(v['seconds'] for v in baseline.values()),
            'cross_language_encryption': 'raw256_and_normalized_plda128_verified',
            'wrong_capture_binding_rejected': True, 'child_lease_preserved': True,
            'production_adapter_scope_reused_one_worker': True,
            'diarization_after_embeddings_repeatable': True, 'local_cost_usd': None, 'energy_wh': None,
            'short_clip_baseline_error': 'speaker_segment_invalid', 'short_clip_physical_bounds_fixed_in_cli_and_resident': True,
            'source_sha256': {str(p.relative_to(ROOT)): sha(p) for p in [Path(__file__), ROOT/'synth/worker/speaker_session.py',
                ROOT/'synth/speakers/native.py', ROOT/'synth/speakers/native/Sources/SynthSpeakers/main.swift',
                ROOT/'synth/speakers/native/Sources/SynthSpeakers/ResidentSpeakers.swift',
                ROOT/'synth/speakers/native/Sources/SynthSpeakers/PhysicalSegments.swift',
                ROOT/'synth/speakers/observations.py', ROOT/'synth/worker/voice_binding.py', ROOT/'synth/speakers/matching.py']},
            'limitations': ['Small public set with oracle crop boundaries; not employee calibration or live diarization accuracy.',
                'Same recognition decisions, no accuracy improvement claimed.',
                'Resident reuse is wired into final profile matching; live Qwen observation/UI integration remains pending.',
                'Current employee profiles and actual document API are not used in this replay.',
                'Helper built against cached pinned FluidAudio with TTS trait disabled; final distribution build remains pending.',
                'Auxiliary encryption/projection/diarization checks are included in total_seconds and excluded from case_embedding_seconds.',
                'Earlier rejected variant and diagnostic operations are preserved separately, not included in this replay timing.']}
        save(args.output, report)
        print(json.dumps({k: v for k, v in report.items() if k not in ('signature', 'baseline', 'source_sha256', 'limitations')}))
    finally:
        if runtime is not None:
            runtime.close()


if __name__ == '__main__':
    main()
