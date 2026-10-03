#!/usr/bin/env python3
"""Fetch hash-pinned public acoustic assets and build the standalone Whisper CLI."""
import os
import hashlib
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
REVISION = '306c88f4d1286aec1bf96e544632897886af5501'
MODEL_SHA = '394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2'


def run(*args):
    os.environ['PATH'] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')
    return subprocess.run((['rtk', 'proxy'] if shutil.which('rtk') else []) + list(args), cwd=ROOT, check=True)


def main():
    if sys.platform != 'darwin': raise SystemExit('The acoustic package currently requires Apple Silicon macOS 14+')
    models = ROOT/'synth/.runtime/models'
    models.mkdir(parents=True, exist_ok=True)
    model = models/'ggml-large-v3-turbo-q5_0.bin'
    if not model.exists():
        context = ssl.create_default_context(cafile='/etc/ssl/cert.pem')
        with urllib.request.urlopen('https://huggingface.co/ggerganov/whisper.cpp/resolve/main/'+model.name,
                                    context=context, timeout=180) as response, model.with_suffix('.part').open('wb') as output:
            shutil.copyfileobj(response, output)
        model.with_suffix('.part').replace(model)
    with model.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != MODEL_SHA:
            raise SystemExit('ASR model checksum mismatch; refusing to use it')
    run(sys.executable, str(ROOT/'synth/scripts/download-speaker-models.py'))
    for source, name in [('NOTICE.md','speaker-model-NOTICE.md'),('provenance.json','speaker-model-PROVENANCE.md')]:
        shutil.copy2(models/'speakers-community1'/source, ROOT/'synth/.runtime'/name)
    source = ROOT/'synth/.toolchains/whisper-portable'
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        run('git', 'clone', 'https://github.com/ggml-org/whisper.cpp.git', str(source))
    observed = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if observed != REVISION:
        run('git', '-C', str(source), 'checkout', '--detach', REVISION)
    run('cmake', '-S', str(source), '-B', str(ROOT/'synth/.runtime/whisper-portable-build'),
        '-DBUILD_SHARED_LIBS=OFF', '-DGGML_METAL=ON', '-DGGML_METAL_EMBED_LIBRARY=ON',
        '-DWHISPER_BUILD_TESTS=OFF', '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0')
    run('cmake', '--build', str(ROOT/'synth/.runtime/whisper-portable-build'), '--target', 'whisper-cli', '-j', '4')
    print('Public acoustic assets and standalone Whisper are ready')


if __name__ == '__main__': main()
