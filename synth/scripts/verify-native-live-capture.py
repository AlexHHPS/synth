#!/usr/bin/env python3
"""Run the native archive/Qwen/cursor/sink roundtrip on explicit public files.

Uses the installed model configuration without editing it. Cargo/build cache
environment is inherited. No device access or network inference is performed.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed-root', type=Path, required=True)
    parser.add_argument('--public-sources', type=Path, required=True)
    parser.add_argument('--cargo', default='cargo')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--speaker-binary', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    input_hashes = {}
    with tempfile.TemporaryDirectory(prefix='synth-public-capture-') as directory:
        temporary = Path(directory)
        fixtures = temporary / 'fixtures'
        fixtures.mkdir(mode=0o700)
        for source in ('microphone', 'system'):
            source_path = args.public_sources / f'{source}.wav'
            if not source_path.is_file():
                source_path = args.public_sources / f'{source}.wav.part'
            # Copy into a disposable, private directory; never rename or modify
            # the source capture or its replay watermark.
            destination = fixtures / f'{source}.wav'
            shutil.copyfile(source_path, destination)
            destination.chmod(0o600)
            with source_path.open('rb') as stream:
                input_hashes[source] = hashlib.file_digest(stream, 'sha256').hexdigest()
        resources = temporary / 'resources'
        internal = resources / 'synth-host/_internal'
        internal.mkdir(parents=True)
        if args.speaker_binary is None:
            (internal / 'synth').symlink_to(root / 'synth', target_is_directory=True)
        else:
            # Assemble the same isolated source allowlist as the DMG packager.
            import sys
            sys.path.insert(0, str(root))
            from synth.worker.live_bundle import FILES
            worker = internal/'synth/worker'
            worker.mkdir(parents=True)
            for name in FILES:
                shutil.copyfile(root/'synth/worker'/name, worker/name)
            installed = Path('/Applications/Synth.app/Contents/Resources/synth-host/_internal')
            model_target = internal/'synth/.runtime/models'
            model_target.mkdir(parents=True)
            (model_target/'speakers-community1').symlink_to(installed/'synth/.runtime/models/speakers-community1')
            binary_target = internal/'synth/.runtime/swift-build/release'
            binary_target.mkdir(parents=True)
            (binary_target/'synth-speakers').symlink_to(args.speaker_binary.resolve(strict=True))
            lock_target = internal/'synth/resources'
            lock_target.mkdir(parents=True)
            shutil.copyfile(installed/'synth/resources/speaker-model-lock.json', lock_target/'speaker-model-lock.json')
        env = {**os.environ, 'SYNTH_QWEN_INSTALLED_ROOT': str(args.installed_root.resolve()),
               'SYNTH_QWEN_TEST_RESOURCES': str(resources), 'SYNTH_QWEN_PUBLIC_CAPTURE': str(fixtures)}
        if args.speaker_binary is not None:
            env['SYNTH_QWEN_TEST_VOICE'] = '1'
        command = [args.cargo, 'test', '--lib', 'real_qwen_capture_roundtrip', '--', '--ignored', '--nocapture']
        result = subprocess.run(command, cwd=root / 'frontend/src-tauri', env=env, capture_output=True, text=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.with_suffix('.log').write_text(result.stdout + result.stderr)
        args.output.with_suffix('.log').chmod(0o600)
        if result.returncode:
            print('NATIVE_CAPTURE_FAILED; inspect', args.output.with_suffix('.log'))
            return result.returncode
        proofs = [line.split('NATIVE_QWEN_CAPTURE_PROOF ', 1)[1] for line in result.stdout.splitlines() if 'NATIVE_QWEN_CAPTURE_PROOF ' in line]
        if len(proofs) != 1:
            raise ValueError('native_capture_proof_missing')
        proof = json.loads(proofs[0])
        proof.update(verified_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), input_sha256=input_hashes,
                     source_sha256={str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in [
                         Path(__file__), root/'frontend/src-tauri/src/audio/transcription/qwen_provider.rs',
                         root/'frontend/src-tauri/src/audio/transcription/capture_stream.rs',
                         root/'frontend/src-tauri/src/audio/source_archive.rs', root/'frontend/src-tauri/src/audio/recording_saver.rs',
                         root/'synth/worker/qwen_live_runner.py', root/'synth/worker/incremental_audio.py',
                         root/'synth/worker/contextual_audio.py', root/'synth/worker/consolidation.py',
                         root/'synth/worker/consolidated_capture.py', root/'synth/worker/word_seams.py',
                         root/'synth/worker/qwen_runner.py', root/'synth/worker/echo.py',
                         root/'synth/worker/voice_activity.py', root/'synth/worker/qwen-model-lock.json']})
        if args.speaker_binary is not None:
            proof['speaker_binary_sha256'] = hashlib.sha256(args.speaker_binary.read_bytes()).hexdigest()
            proof['voice_source_sha256'] = {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in
                ('synth/worker/live_voice.py', 'synth/worker/voice_windows.py', 'synth/worker/voice_key.py',
                 'synth/worker/speaker_session.py', 'synth/worker/voice_binding.py')}
        args.output.write_text(json.dumps(proof, indent=2)+'\n')
        print(json.dumps(proof))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
