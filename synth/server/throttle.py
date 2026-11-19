"""Refuse an address that keeps presenting credentials nobody issued.

Every refused credential costs a database connection and, with corporate
sign-in, a question to the identity service. Without a limit, a few dozen
junk requests at once exhaust both for everyone. Signed-in use is never
counted: only answers that were 401.
"""
import threading
import time

WINDOW_SECONDS = 60
REFUSALS = 40
_refused = {}
_lock = threading.Lock()


def client(request):
    # The platform's proxy appends the address it saw; earlier entries are
    # whatever the caller chose to send.
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
    return forwarded or (request.client.host if request.client else "")


def blocked(address, now=None):
    if not address or address in ("127.0.0.1", "::1", "testclient"):
        return False
    now = time.monotonic() if now is None else now
    with _lock:
        recent = [at for at in _refused.get(address, ()) if now - at < WINDOW_SECONDS]
        if recent:
            _refused[address] = recent
        else:
            _refused.pop(address, None)
        return len(recent) >= REFUSALS


def refused(address, now=None):
    if not address:
        return
    now = time.monotonic() if now is None else now
    with _lock:
        if len(_refused) > 10000:
            _refused.clear()
        _refused.setdefault(address, []).append(now)


def reset():
    with _lock:
        _refused.clear()
