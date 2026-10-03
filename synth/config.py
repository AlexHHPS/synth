"""Deployment configuration supplied by the operator, never by transcript content."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

DEFAULTS = json.loads(Path(__file__).with_name('deployment.json').read_text())


def origin(value, *, loopback=False):
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('configuration_origin_invalid')
    if parsed.scheme != 'https' and not (loopback and value == 'http://127.0.0.1:18280'):
        raise ValueError('configuration_https_required')
    if not parsed.hostname:
        raise ValueError('configuration_host_required')
    return value.rstrip('/')


API_URL = origin(os.environ.get('SYNTH_API_URL', DEFAULTS['api_url']), loopback=True)
SUPABASE_URL = os.environ.get('SUPABASE_URL', DEFAULTS.get('supabase_url', '')).rstrip('/')
if SUPABASE_URL:
    SUPABASE_URL = origin(SUPABASE_URL)
AUTH_MODE = os.environ.get('AUTH_MODE', DEFAULTS.get('auth_mode', 'keys'))

if AUTH_MODE not in {'keys', 'supabase'}:
    raise ValueError('configuration_auth_mode_invalid')
if AUTH_MODE == 'supabase' and not SUPABASE_URL:
    raise ValueError('configuration_supabase_origin_required')
