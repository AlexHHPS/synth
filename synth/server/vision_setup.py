"""Expose preparation of the optional visual model through the desktop host."""
import logging
import threading

from synth.worker import media_vision
from synth.worker.desktop_pipeline import APPDATA

_LOCK = threading.Lock()
_THREAD = None
_FAILED = False


def status(appdata=APPDATA):
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            state = "preparing"
        elif media_vision.ready(appdata):
            state = "ready"
        else:
            state = "failed" if _FAILED else "missing"
        return {"state": state, "model": media_vision.MODEL_ID}


def start(appdata=APPDATA):
    global _THREAD, _FAILED

    def prepare():
        global _FAILED
        try:
            media_vision.setup(appdata)
        except Exception:
            logging.getLogger(__name__).exception("Local visual model setup failed")
            with _LOCK:
                _FAILED = True

    with _LOCK:
        if not (_THREAD and _THREAD.is_alive()) and not media_vision.ready(appdata):
            _FAILED = False
            _THREAD = threading.Thread(target=prepare, daemon=True, name="synth-vision-setup")
            _THREAD.start()
    return status(appdata)
