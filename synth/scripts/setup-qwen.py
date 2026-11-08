"""Prepare the pinned audio runtime from the packaged app or a checkout."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT/'synth/worker/qwen-model-lock.json'
REQUIREMENTS = ROOT/'synth/worker/qwen-runtime.requirements.txt'


def write_json(path, value):
    part = path.with_name(path.name+'.part')
    fd = os.open(part,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    part.replace(path)


def assert_idle(appdata,allow_queued=False):
    database=appdata/'pipeline/queue.sqlite3'
    if database.is_file():
        with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as queue:
            states=('running','cancel_requested') if allow_queued else ('running','queued','cancel_requested')
            count=queue.execute('SELECT COUNT(*) FROM desktop_tasks WHERE state IN ('+','.join('?' for _ in states)+')',states).fetchone()[0]
        if count:raise ValueError('Termina el procesamiento de las reuniones antes de preparar el audio.')
    for receipt in (appdata/'voice/jobs').glob('*.json'):
        if json.loads(receipt.read_text()).get('state')=='running':raise ValueError('Termina el registro de voz antes de preparar el audio.')
    capture=appdata/'capture/raw'
    if capture.is_dir():
        result=subprocess.run(['/usr/sbin/lsof','-nP','+D',str(capture),'-Fn'],capture_output=True,text=True,timeout=20)
        if result.returncode not in (0,1):raise ValueError('No se pudo comprobar si hay una grabación abierta.')
        if any(line.startswith('n') and line.lower().endswith(('.wav','.wav.part','.mp4','.m4a')) for line in result.stdout.splitlines()):
            raise ValueError('Finaliza la grabación antes de preparar el audio.')


def runtime_ready(python, env):
    if not python.is_file():return False
    code="import sys,importlib.metadata as m,json;print(json.dumps({'python':list(sys.version_info[:2]),'packages':{d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions()}}))"
    result=subprocess.run([str(python),'-I','-c',code],capture_output=True,text=True,env=env,timeout=30)
    if result.returncode:return False
    current=json.loads(result.stdout)
    required={line.split('==')[0].lower().replace('_','-'):line.split('==')[1] for line in REQUIREMENTS.read_text().splitlines() if line and not line.startswith('#')}
    return current.get('python')==[3,12] and all(current.get('packages',{}).get(k)==v for k,v in required.items())


def model_file_matches(path, item):
    if not path.is_file() or path.stat().st_size != item['size']:
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest() == item['sha256']


def model_download_plan(appdata, models):
    targets = {role:appdata/'models'/('qwen3-asr-1.7b-bf16' if role=='asr' else 'qwen3-aligner-0.6b-bf16') for role in models}
    missing = {}
    for role,model in models.items():
        files = [item for item in model['files'] if not model_file_matches(targets[role]/item['path'],item)]
        if files:
            missing[role] = {**model,'files':files}
    return targets,missing


def check_installation(appdata):
    current=appdata/'asr-config.json'
    if not current.is_file():return {'ready':False,'reason':'audio_not_prepared'}
    from synth.worker.qwen_asr import verify_runtime
    try:
        configuration=json.loads(current.read_text())
        if configuration.get('backend')!='qwen3' or configuration.get('live_backend')!='qwen3' or configuration.get('echo_cancellation')!='aec3' or configuration.get('voice_embedding_space')!='plda_rho128_v1':
            return {'ready':False,'reason':'audio_configuration_incomplete'}
        verify_runtime(configuration)
        if not runtime_ready(Path(configuration['python']),dict(os.environ)):return {'ready':False,'reason':'audio_runtime_incomplete'}
    except (ValueError,OSError,KeyError):return {'ready':False,'reason':'audio_assets_incomplete'}
    return {'ready':True,'backend':'qwen3','live_backend':'qwen3','echo_cancellation':'aec3','voice_embedding_space':'plda_rho128_v1'}


def main(argv=None,on_progress=None,allow_queued=False):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activate',action='store_true')
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--app-data',type=Path,default=Path.home()/'Library/Application Support/dev.synth.voice')
    args=parser.parse_args(argv);appdata=args.app_data.expanduser().resolve()
    if args.check:
        result=check_installation(appdata);print(json.dumps(result));return 0 if result['ready'] else 2
    if platform.system()!='Darwin' or platform.machine()!='arm64':raise ValueError('Este paquete necesita un Mac con Apple Silicon.')
    if tuple(map(int,platform.mac_ver()[0].split('.')[:2]))<(14,2):raise ValueError('Este paquete necesita macOS 14.2 o posterior.')
    if not shutil.which('uv'):raise ValueError('No se encuentra el preparador incluido en la app. Vuelve a copiarla desde el DMG.')
    appdata.mkdir(parents=True,exist_ok=True,mode=0o700)
    from synth.worker.desktop_pipeline import inference_lock
    from synth.worker.subprocesses import run_process
    with inference_lock(appdata/'pipeline') as lock_fd:
        assert_idle(appdata,allow_queued)
        env={**os.environ,'SSL_CERT_FILE':'/etc/ssl/cert.pem','HF_HUB_DISABLE_TELEMETRY':'1','HF_HUB_DISABLE_XET':'1',
             'HF_HUB_OFFLINE':'0','HF_HUB_CACHE':str(appdata/'models/.downloads'),
             'UV_CACHE_DIR':str(appdata/'runtime/.uv-cache'),'UV_PYTHON_INSTALL_DIR':str(appdata/'runtime/python')}
        runtime=appdata/'runtime/qwen-asr';python=runtime/'bin/python'
        lock=json.loads(LOCK.read_text())
        targets,missing=model_download_plan(appdata,lock['models'])
        runtime_complete=runtime_ready(python,env)
        required_bytes=sum(item['size'] for model in missing.values() for item in model['files'])
        # Leave headroom for the application, ongoing system writes and swap.
        required_bytes += (0 if runtime_complete else 1_000_000_000) + 2_000_000_000
        if shutil.disk_usage(appdata).free < required_bytes:
            raise ValueError(f'Libera al menos {required_bytes/1_000_000_000:.1f} GB antes de preparar el audio.')
        def progress(step,message):
            print(f'{step}/3 {message}',flush=True)
            if on_progress:on_progress(step,message)
        progress(1,'Preparando el runtime de audio local…')
        if not python.is_file():
            run_process(['uv','venv','--python','3.12','--python-preference','only-managed','--allow-existing',str(runtime)],check=True,env=env,timeout=600,pass_fds=(lock_fd,))
        if not runtime_complete:
            run_process(['uv','pip','install','--python',str(python),'--only-binary',':all:','--requirement',str(REQUIREMENTS)],check=True,env=env,timeout=1800,pass_fds=(lock_fd,))
        if not runtime_ready(python,env):raise ValueError('No se pudo verificar el runtime descargado.')
        progress(2,'Descargando los modelos que faltan…' if missing else 'Reutilizando los modelos ya descargados y verificados…')
        downloader="""
import json,sys
from huggingface_hub import snapshot_download
models=json.load(sys.stdin)
paths={key:snapshot_download(model['repo'],revision=model['revision'],allow_patterns=[f['path'] for f in model['files']],max_workers=2,token=False) for key,model in models.items()}
print(json.dumps(paths))
"""
        sources={}
        if missing:
            result=run_process([str(python),'-I','-c',downloader],input=json.dumps(missing),text=True,stdout=subprocess.PIPE,check=True,env=env,timeout=3600,pass_fds=(lock_fd,))
            sources=json.loads(result.stdout)
        config={'backend':'qwen3','live_backend':'qwen3','python':str(python),'echo_cancellation':'aec3','voice_embedding_space':'plda_rho128_v1'}
        for role,model in lock['models'].items():
            target=targets[role]
            target.mkdir(parents=True,exist_ok=True,mode=0o700)
            for item in model['files']:
                destination=target/item['path']
                if not model_file_matches(destination,item):
                    source=(Path(sources[role])/item['path']).resolve(strict=True)
                    if not model_file_matches(source,item):raise ValueError('No coincide la integridad de un modelo de audio.')
                    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                    temporary=destination.with_name(destination.name+'.verified.part')
                    if temporary.exists():temporary.unlink()
                    try:os.link(source,temporary)
                    except OSError:shutil.copy2(source,temporary)
                    temporary.replace(destination)
            config[role]=str(target)
        pending=appdata/'asr-config.qwen.pending.json';write_json(pending,config)
        progress(3,'Activando Qwen, cancelación de eco y comparación de voces…')
        if args.activate:
            assert_idle(appdata,allow_queued)
            current=appdata/'asr-config.json'
            if current.exists():shutil.copy2(current,appdata/('asr-config.before-setup-'+time.strftime('%Y%m%d-%H%M%S')+'.json'))
            pending.replace(current)
        print('AUDIO_PREPARADO' if args.activate else 'AUDIO_LISTO_PARA_ACTIVAR',flush=True)
    return 0


if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,OSError,subprocess.SubprocessError) as error:
        print('No se completó la preparación del audio. '+(str(error) if isinstance(error,ValueError) else 'Revisa la conexión y vuelve a ejecutar el preparador.'),file=sys.stderr)
        sys.exit(1)
