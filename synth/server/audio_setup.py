"""Prepare Qwen on first launch before any capture can enter the acoustic worker."""
import importlib.util
import json
from pathlib import Path
import threading

from synth.worker.desktop_pipeline import APPDATA

_GATE = threading.Lock()
_THREAD = None
_STATE = {'state':'ready','step':3,'message':'Audio preparado'}


def load_installer():
    path=Path(__file__).resolve().parents[1]/'scripts/setup-qwen.py'
    spec=importlib.util.spec_from_file_location('synth_audio_installer',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def status():
    with _GATE:return dict(_STATE)


def ready():
    return status()['state']=='ready'


def start(appdata=APPDATA):
    global _THREAD
    appdata=Path(appdata)
    with _GATE:
        if _THREAD is not None and _THREAD.is_alive():return dict(_STATE)
        _STATE.update(state='checking',step=0,message='Comprobando el audio de este Mac')
        def prepare():
            try:
                installer=load_installer()
                if not installer.check_installation(appdata)['ready']:
                    def progress(step,message):
                        with _GATE:_STATE.update(state='preparing',step=step,message=message)
                    result=installer.main(['--activate','--app-data',str(appdata)],on_progress=progress,allow_queued=True)
                    if result or not installer.check_installation(appdata)['ready']:raise ValueError('audio_setup_incomplete')
                with _GATE:_STATE.update(state='ready',step=3,message='Audio preparado')
            except Exception:
                with _GATE:_STATE.update(state='failed',message='No se pudo preparar el audio. Comprueba la conexión, termina cualquier grabación y reintenta.')
        _THREAD=threading.Thread(target=prepare,daemon=True,name='synth-audio-setup')
        _THREAD.start()
        return dict(_STATE)
