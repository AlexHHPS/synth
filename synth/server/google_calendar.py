"""Read-only personal calendar. Supabase brokers consent; provider tokens stay server-side.

Full bounded replacement is deliberate: 14 days, expanded recurring instances,
pagination and atomic publish. Never combine a moving timeMin with syncToken.
Calendar guests and descriptions are context, never acoustic/meeting evidence.
"""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import ssl
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from .db import authorize_folder, audit

SCOPE = 'https://www.googleapis.com/auth/calendar.events.readonly'
GOOGLE = 'https://www.googleapis.com'
TOKEN = 'https://oauth2.googleapis.com/token'


def require_user(actor):
    if actor['kind'] != 'user' or str(actor['id']) == '00000000-0000-4000-8000-000000000001':
        raise HTTPException(403, 'Conecta el calendario con tu cuenta personal de Synth.')


def configured():
    secret = os.environ.get('SYNTH_GOOGLE_CLIENT_SECRET', '')
    # Management APIs may return an opaque 64-hex representation, not a reusable
    # provider secret. Never offer another consent loop with this known bad value.
    return bool(os.environ.get('SYNTH_GOOGLE_CLIENT_ID') and secret and not re.fullmatch('[0-9a-f]{64}', secret)
                and os.environ.get('VOICE_PROFILE_ENCRYPTION_KEY'))


def cipher():
    try:
        root = base64.b64decode(os.environ['VOICE_PROFILE_ENCRYPTION_KEY'], validate=True)
        if len(root) != 32:
            raise ValueError()
    except (KeyError, ValueError):
        raise HTTPException(503, 'El conector de calendario no está configurado.') from None
    # Separate encryption domain from biometric profiles, even with the same managed root.
    return AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                       info=b'synth-voice/google-calendar/tokens/v1').derive(root))


def seal(owner, value):
    nonce = os.urandom(12)
    return nonce, cipher().encrypt(nonce, json.dumps(value).encode(), str(owner).encode())


def unseal(row):
    return json.loads(cipher().decrypt(bytes(row['nonce']), bytes(row['ciphertext']), str(row['owner_id']).encode()))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def google(path, *, token=None, form=None):
    if path != TOKEN and not path.startswith((GOOGLE + '/calendar/v3/', GOOGLE + '/oauth2/v3/userinfo')):
        raise ValueError('calendar_destination_denied')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if form is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    request = urllib.request.Request(path, headers=headers,
        data=urllib.parse.urlencode(form).encode() if form is not None else None)
    try:
        with opener.open(request, timeout=20) as response:
            data = response.read(4_000_001)
            if len(data) > 4_000_000:
                raise ValueError('calendar_response_limit')
            return json.loads(data)
    except urllib.error.HTTPError as error:
        # Never include Google's body, authorization code, refresh token or URL in errors.
        body = {}
        try:
            body = json.loads(error.read(16384))
            reasons = {item.get('reason') for item in body.get('error', {}).get('errors', [])}
            reasons |= {item.get('reason') for item in body.get('error', {}).get('details', [])}
        except (ValueError, AttributeError, TypeError):
            reasons = set()
            if not isinstance(body, dict):
                body = {}
        if isinstance(body.get('error'), str) and body['error'] == 'invalid_client':
            raise HTTPException(503, 'calendar_client_configuration_invalid') from None
        if reasons.intersection({'accessNotConfigured', 'SERVICE_DISABLED'}):
            raise HTTPException(409, 'calendar_api_not_enabled') from None
        raise HTTPException(409 if error.code in (400, 401, 403) else 503,
            'calendar_reconnect_required' if error.code in (400, 401, 403) else 'calendar_google_unavailable') from None
    except (OSError, ValueError):
        raise HTTPException(503, 'calendar_google_unavailable') from None


class ConnectionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    access_token: str = Field(min_length=10, max_length=8192)
    refresh_token: str = Field(min_length=10, max_length=8192)


def connect_google(db, actor, body):
    require_user(actor)
    if not configured():
        raise HTTPException(503, 'El conector de calendario no está configurado.')
    identity = google(GOOGLE + '/oauth2/v3/userinfo', token=body.access_token)
    if not identity.get('email_verified') or identity.get('email', '').casefold() != (actor.get('email') or actor['name']).casefold():
        raise HTTPException(403, 'Conecta la misma cuenta de Google con la que has entrado en Synth.')
    # Refresh immediately: proves the offline grant is usable with this application's client.
    value = google(TOKEN, form={'client_id': os.environ['SYNTH_GOOGLE_CLIENT_ID'],
        'client_secret': os.environ['SYNTH_GOOGLE_CLIENT_SECRET'],
        'refresh_token': body.refresh_token, 'grant_type': 'refresh_token'})
    if value.get('scope') and SCOPE not in value['scope'].split() and 'https://www.googleapis.com/auth/calendar.readonly' not in value['scope'].split():
        raise HTTPException(409, 'calendar_read_permission_required')
    # Permission/API-enabled check before replacing a working connection.
    google(GOOGLE + '/calendar/v3/calendars/primary/events?maxResults=1', token=value['access_token'])
    nonce, ciphertext = seal(actor['id'], {'refresh_token': body.refresh_token})
    db.execute('''INSERT INTO calendar_connections(owner_id,email,nonce,ciphertext)
        VALUES (%s,%s,%s,%s) ON CONFLICT(owner_id) DO UPDATE SET email=excluded.email,
        nonce=excluded.nonce,ciphertext=excluded.ciphertext,revision=calendar_connections.revision+1,
        synced_at=NULL,updated_at=now()''', (actor['id'], identity['email'], nonce, ciphertext))
    db.execute('DELETE FROM calendar_events WHERE owner_id=%s', (actor['id'],))
    audit(db, actor['id'], 'calendar.connect', actor['id'])
    return {'connected': True, 'email': identity['email']}


def plain(value, maximum=2000):
    # Render imported HTML as text, never as markup or instructions.
    import html
    return html.unescape(re.sub('<[^>]*>', ' ', str(value or '')))[:maximum].strip()


def normalize(event):
    start, end = event.get('start', {}), event.get('end', {})
    if event.get('eventType') == 'workingLocation':
        return None  # Calendar presence (e.g. home/office) is not a meeting.
    if event.get('status') == 'cancelled' or not event.get('id') or not (start.get('dateTime') or start.get('date')):
        return None
    attendees = [{'email': str(a.get('email', ''))[:320], 'name': plain(a.get('displayName'), 200),
                  'response': a.get('responseStatus', 'needsAction'), 'self': bool(a.get('self'))}
                 for a in event.get('attendees', [])[:200] if not a.get('resource')]
    if any(a['self'] and a['response'] == 'declined' for a in attendees):
        return None
    return {'title': plain(event.get('summary'), 300) or 'Sin título',
            'description': plain(event.get('description'), 4000),
            'start': start.get('dateTime') or start['date'], 'end': end.get('dateTime') or end.get('date'),
            'all_day': 'date' in start, 'timezone': start.get('timeZone'),
            'attendees': attendees, 'attendees_omitted': bool(event.get('attendeesOmitted')),
            'location': plain(event.get('location'), 500),
            'recurring_event_id': event.get('recurringEventId'), 'provider_updated': event.get('updated'),
            'provenance': 'google_calendar', 'identities_confirmed': False}


def synchronize(db, actor, *, force=False):
    require_user(actor)
    # Serialize refresh/sync/disconnect and reconnect for this actor. No cross-user lock.
    row = db.execute('SELECT * FROM calendar_connections WHERE owner_id=%s FOR UPDATE', (actor['id'],)).fetchone()
    if not row:
        raise HTTPException(409, 'Conecta Google Calendar para sincronizar.')
    now = datetime.now(timezone.utc)
    if not force and row['synced_at'] and (now-row['synced_at']).total_seconds() < 240:
        return {'synced_at': row['synced_at'], 'cached': True}
    secret = unseal(row)
    value = google(TOKEN, form={'client_id': os.environ.get('SYNTH_GOOGLE_CLIENT_ID', ''),
        'client_secret': os.environ.get('SYNTH_GOOGLE_CLIENT_SECRET', ''),
        'refresh_token': secret['refresh_token'], 'grant_type': 'refresh_token'})
    token = value['access_token']
    query = {'timeMin': now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
             'timeMax': (now+timedelta(days=14)).isoformat(), 'singleEvents': 'true',
             'orderBy': 'startTime', 'maxResults': '250', 'showDeleted': 'false'}
    events, seen_pages = [], set()
    for _ in range(20):
        result = google(GOOGLE + '/calendar/v3/calendars/primary/events?' + urllib.parse.urlencode(query), token=token)
        events += result.get('items', [])
        page = result.get('nextPageToken')
        if not page:
            break
        if page in seen_pages:
            raise HTTPException(503, 'calendar_pagination_invalid')
        seen_pages.add(page)
        query['pageToken'] = page
    else:
        raise HTTPException(409, 'El calendario supera el límite de 5.000 eventos en dos semanas.')
    # No partial publish when Google fails midway; removed/cancelled events disappear atomically.
    db.execute('UPDATE calendar_events SET active=false,prepared_pack=NULL,prepared_at=NULL WHERE owner_id=%s', (actor['id'],))
    count = 0
    for raw in events:
        content = normalize(raw)
        if content is None:
            continue
        count += 1
        fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        db.execute('''INSERT INTO calendar_events(id,owner_id,provider_id,content,content_hash,starts_at,ends_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(owner_id,provider_id) DO UPDATE
            SET content=excluded.content,content_hash=excluded.content_hash,starts_at=excluded.starts_at,
                ends_at=excluded.ends_at,active=true,updated_at=now()''',
            (uuid4(), actor['id'], raw['id'], Jsonb(content), fingerprint, content['start'], content['end']))
    db.execute('UPDATE calendar_connections SET synced_at=%s WHERE owner_id=%s', (now, actor['id']))
    # Prepare the next few meetings while there is time, without a model call.
    upcoming = db.execute('''SELECT id FROM calendar_events WHERE owner_id=%s AND active AND ends_at>now()
        ORDER BY starts_at,id LIMIT 3''', (actor['id'],)).fetchall()
    for event in upcoming:
        try:
            preparation(db, actor, event['id'])
        except HTTPException as error:
            if error.status_code not in (403,404):
                raise
    return {'synced_at': now, 'events': count, 'cached': False}


def event_row(db, actor, event_id):
    require_user(actor)
    row = db.execute('''SELECT e.* FROM calendar_events e JOIN calendar_connections c ON c.owner_id=e.owner_id
        WHERE e.id=%s AND e.owner_id=%s AND e.active''', (event_id, actor['id'])).fetchone()
    if not row:
        raise HTTPException(404, 'El evento ya no está disponible. Sincroniza tu calendario.')
    return row


def name_key(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value.casefold()) if not unicodedata.combining(c)).strip()


def preparation(db, actor, event_id):
    """Fresh permission-bound pack; extractive briefing without unsupported model claims."""
    from .glossary import visible_terms
    from .questions import retrieve
    row = event_row(db, actor, event_id)
    folder = authorize_folder(db, actor, row['folder_id']) if row['folder_id'] else None
    event = row['content']
    directory = db.execute("SELECT id,name FROM principals WHERE kind='user' AND revoked_at IS NULL").fetchall()
    profiles = {str(p['owner_id']) for p in db.execute('SELECT DISTINCT owner_id FROM voice_profiles').fetchall()}
    guests = []
    for guest in event['attendees']:
        candidates = [p for p in directory if name_key(p['name']) in {name_key(guest['email']), name_key(guest['name'])}]
        person = candidates[0] if len(candidates) == 1 else None
        guests.append({**guest, 'principal_id': str(person['id']) if person else None,
                       'profile_available': bool(person and str(person['id']) in profiles),
                       'provenance': 'calendar_invitation', 'voice_confirmed': False})
    vocabulary = visible_terms(db, actor, row['folder_id'], 'aprobado')
    glossary = [{'id': str(g['id']), 'term': g['term'], 'definition': (g['definition'] or '')[:600],
                 'aliases': g['aliases'][:5], 'revision': g['updated_at'].isoformat(), 'provenance': 'approved_glossary'}
                for g in sorted(vocabulary, key=lambda g: (-g['occurrences'], g['term']))[:30]]
    passages = retrieve(db, actor, event['title'][:500], folder_id=row['folder_id'],
        created_before=row['starts_at'])[:8] if folder else []
    for passage in passages:
        passage['provenance'] = 'historical_transcript'
        passage['evidence_id'] = f"{passage['meeting_id']}@{passage['transcript_version']}:{passage['turn_id']}"
    pack = {'event_id': str(row['id']), 'event_hash': row['content_hash'], 'event': event,
        'folder_id': str(row['folder_id']) if folder else None, 'folder': folder['name'] if folder else None,
        'expected_attendees': guests, 'glossary': glossary, 'passages': passages,
        'policy': 'calendar_preparation_v1', 'requires_fresh_authorization_before_reuse': True,
        'identities_confirmed': False, 'calendar_is_meeting_evidence': False}
    pack['fingerprint'] = hashlib.sha256(json.dumps(pack, sort_keys=True, default=str).encode()).hexdigest()
    # Cache is private and disposable. Every consumer revalidates source access;
    # the fingerprint and this stored copy never confer authorization.
    db.execute('UPDATE calendar_events SET prepared_pack=%s,prepared_at=now() WHERE id=%s AND owner_id=%s',
               (Jsonb(pack), event_id, actor['id']))
    return pack


class FolderIn(BaseModel):
    folder_id: UUID | None = None


class SyncIn(BaseModel):
    force: bool = False


def register(app, session_dependency):
    @app.get('/v1/calendar')
    def calendar_state(context: session_dependency):
        db, actor = context
        require_user(actor)
        row = db.execute('SELECT email,synced_at FROM calendar_connections WHERE owner_id=%s', (actor['id'],)).fetchone()
        events = db.execute('''SELECT id,content,folder_id,content_hash,prepared_at FROM calendar_events
            WHERE owner_id=%s AND active AND ends_at>now() ORDER BY starts_at,id LIMIT 500''', (actor['id'],)).fetchall() if row else []
        return {'configured': configured(), 'connected': bool(row), 'email': row['email'] if row else None,
                'synced_at': row['synced_at'] if row else None, 'items': events, 'read_only': True}

    @app.post('/v1/calendar/connection')
    def connection(body: ConnectionIn, context: session_dependency):
        return connect_google(*context, body)

    @app.post('/v1/calendar/sync')
    def sync(context: session_dependency, body: SyncIn = SyncIn()):
        return synchronize(*context, force=body.force)

    @app.delete('/v1/calendar/connection')
    def disconnect(context: session_dependency):
        db, actor = context
        require_user(actor)
        db.execute('DELETE FROM calendar_connections WHERE owner_id=%s', (actor['id'],))
        audit(db, actor['id'], 'calendar.disconnect', actor['id'])
        # Local grant/cache removed; don't revoke Google's combined sign-in grant.
        return {'connected': False}

    @app.get('/v1/calendar/events/{event_id}/preparation')
    def prepare(event_id: UUID, context: session_dependency):
        return preparation(*context, event_id)

    @app.put('/v1/calendar/events/{event_id}/folder')
    def assign(event_id: UUID, body: FolderIn, context: session_dependency):
        db, actor = context
        event_row(db, actor, event_id)
        if body.folder_id:
            authorize_folder(db, actor, body.folder_id, write=True)
        db.execute('UPDATE calendar_events SET folder_id=%s WHERE id=%s AND owner_id=%s', (body.folder_id, event_id, actor['id']))
        return preparation(db, actor, event_id)
