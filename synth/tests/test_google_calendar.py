"""Calendar normalization, credential isolation and the real DB/permission boundary.

Google replies are synthetic; this does not claim a live Google consent run.
"""
import base64
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from synth.server import google_calendar as cal


def event(key='first', title='Nimbus permisos'):
    return {'id': key, 'summary': title, 'description': '<p>Revisar permisos</p>',
        'start': {'dateTime': (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()},
        'end': {'dateTime': (datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()},
        'attendees': [{'email': 'alice@example.test', 'displayName': 'Alice'}]}


class CalendarTests(unittest.TestCase):
    def test_work_locations_do_not_become_meetings_but_real_all_day_events_remain(self):
        raw={**event(title='Casa'),'start':{'date':'2026-11-10'},'end':{'date':'2026-11-11'}}
        self.assertIsNone(cal.normalize({**raw,'eventType':'workingLocation'}))
        self.assertEqual(cal.normalize({**raw,'eventType':'default'})['title'],'Casa')
    def test_obfuscated_management_secret_is_not_accepted_as_a_google_credential(self):
        with patch.dict(os.environ, {'SYNTH_GOOGLE_CLIENT_ID':'synthetic.apps.googleusercontent.com',
                'SYNTH_GOOGLE_CLIENT_SECRET':'a'*64,'VOICE_PROFILE_ENCRYPTION_KEY':'configured'}):
            self.assertFalse(cal.configured())

    def test_invalid_client_is_configuration_error_not_another_consent_loop(self):
        import io
        import urllib.error
        error = urllib.error.HTTPError(cal.TOKEN, 401, 'unauthorized', {}, io.BytesIO(b'{"error":"invalid_client"}'))
        with patch.object(cal.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaises(HTTPException) as caught:
                cal.google(cal.TOKEN,form={'client_id':'synthetic'})
        self.assertEqual(caught.exception.detail,'calendar_client_configuration_invalid')

    def test_cancelled_declined_and_resources_are_not_meetings(self):
        raw = event()
        self.assertIsNone(cal.normalize({**raw, 'status': 'cancelled'}))
        self.assertIsNone(cal.normalize({**raw, 'attendees': [{'self': True, 'responseStatus': 'declined'}]}))
        normalized = cal.normalize({**raw, 'attendees': [{'resource': True, 'email': 'room'}, raw['attendees'][0]]})
        self.assertEqual(len(normalized['attendees']), 1)
        self.assertEqual(normalized['description'], 'Revisar permisos')
        self.assertFalse(normalized['identities_confirmed'])

    def test_all_day_timezone_and_separate_recurring_instances(self):
        raw = {**event(), 'start': {'date': '2026-11-10'}, 'end': {'date': '2026-11-11'},
               'recurringEventId': 'parent'}
        value = cal.normalize(raw)
        self.assertTrue(value['all_day'])
        self.assertEqual(value['recurring_event_id'], 'parent')
        self.assertEqual(value['end'], '2026-11-11')

    def test_credentials_are_encrypted_and_bound_to_the_owner(self):
        with patch.dict(os.environ, {'VOICE_PROFILE_ENCRYPTION_KEY': base64.b64encode(b'z'*32).decode()}):
            owner = uuid4()
            nonce, ciphertext = cal.seal(owner, {'refresh_token': 'private-test-token'})
            self.assertNotIn(b'private-test-token', ciphertext)
            row = {'owner_id': owner, 'nonce': nonce, 'ciphertext': ciphertext}
            self.assertEqual(cal.unseal(row), {'refresh_token': 'private-test-token'})
            from cryptography.exceptions import InvalidTag
            with self.assertRaises(InvalidTag):
                cal.unseal({**row, 'owner_id': uuid4()})

    def test_arbitrary_destinations_are_never_requested(self):
        with self.assertRaisesRegex(ValueError, 'destination_denied'):
            cal.google('https://evil.invalid')


@unittest.skipUnless(os.environ.get('SYNTH_TEST_DATABASE_URL'), 'Disposable PostgreSQL required')
class CalendarPostgresTests(unittest.TestCase):
    def setUp(self):
        from synth.tests.test_questions import QuestionFlowTests
        self.fixture = QuestionFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        for name in ('db', 'alice', 'bob', 'folder', 'shared', 'private', 'api'):
            setattr(self, name, getattr(self.fixture, name))
        self.alice = {**self.alice, 'email': 'alice@example.test'}
        self.env = patch.dict(os.environ, {'VOICE_PROFILE_ENCRYPTION_KEY': base64.b64encode(b'z'*32).decode(),
            'SYNTH_GOOGLE_CLIENT_ID': 'synthetic-client', 'SYNTH_GOOGLE_CLIENT_SECRET': 'synthetic-secret'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.add_connection()

    def add_connection(self):
        nonce, ciphertext = cal.seal(self.alice['id'], {'refresh_token': 'synthetic-refresh-token'})
        self.db.execute('''INSERT INTO calendar_connections(owner_id,email,nonce,ciphertext) VALUES (%s,%s,%s,%s)
            ON CONFLICT(owner_id) DO UPDATE SET nonce=excluded.nonce,ciphertext=excluded.ciphertext''',
            (self.alice['id'], self.alice['email'], nonce, ciphertext))

    def sync(self, pages):
        with patch.object(cal, 'google', side_effect=[{'access_token': 'synthetic'}, *pages]) as call:
            result = cal.synchronize(self.db, self.alice, force=True)
            return result, call.call_args_list

    def rows(self):
        return self.db.execute('SELECT * FROM calendar_events WHERE owner_id=%s ORDER BY provider_id', (self.alice['id'],)).fetchall()

    def test_pages_are_complete_recurring_instances_kept_and_cancelled_events_disappear(self):
        result, calls = self.sync([{'items': [event('a')], 'nextPageToken': 'page2'}, {'items': [event('b')]}])
        self.assertEqual(result['events'], 2)
        self.assertIn('singleEvents=true', calls[1].args[0])
        self.assertIn('pageToken=page2', calls[2].args[0])
        self.assertNotIn('syncToken', calls[2].args[0])
        original_id = self.rows()[0]['id']
        self.sync([{'items': [event('a', 'Título modificado'), {**event('b'), 'status': 'cancelled'}]}])
        rows = self.rows()
        self.assertEqual(rows[0]['id'], original_id)
        self.assertEqual(rows[0]['content']['title'], 'Título modificado')
        self.assertFalse(rows[1]['active'])

    def test_failed_second_page_preserves_last_good_calendar(self):
        self.sync([{'items': [event()]}])
        before = self.rows()
        with patch.object(cal, 'google', side_effect=[{'access_token': 'test'},
                {'items': [event('replacement')], 'nextPageToken': 'page2'}, HTTPException(503, 'unavailable')]):
            with self.assertRaises(HTTPException):
                cal.synchronize(self.db, self.alice, force=True)
        self.assertEqual(self.rows(), before)

    def test_foreign_calendar_is_private_even_when_folder_is_shared(self):
        self.sync([{'items': [event()]}])
        key = self.rows()[0]['id']
        self.db.execute('UPDATE calendar_events SET folder_id=%s WHERE id=%s', (self.folder, key))
        self.db.execute("INSERT INTO folder_memberships VALUES (%s,%s,'editor')", (self.folder, self.bob['id']))
        with self.assertRaises(HTTPException) as error:
            cal.preparation(self.db, self.bob, key)
        self.assertEqual(error.exception.status_code, 404)

    def test_pack_is_fresh_after_folder_access_revocation_and_never_confirms_speakers(self):
        self.sync([{'items': [event()]}])
        key = self.rows()[0]['id']
        pack = cal.preparation(self.db, self.alice, key)
        self.assertEqual(pack['passages'], [])
        self.assertFalse(pack['identities_confirmed'])
        self.assertFalse(pack['expected_attendees'][0]['voice_confirmed'])
        external = uuid4()
        self.db.execute("INSERT INTO folders(id,owner_id,name) VALUES (%s,%s,'Contexto externo')", (external, self.bob['id']))
        self.db.execute("INSERT INTO folder_memberships VALUES (%s,%s,'editor')", (external, self.alice['id']))
        self.db.execute('UPDATE calendar_events SET folder_id=%s WHERE id=%s', (external, key))
        first = cal.preparation(self.db, self.alice, key)
        self.assertNotEqual(first['fingerprint'], pack['fingerprint'])
        self.db.execute('DELETE FROM folder_memberships WHERE folder_id=%s AND principal_id=%s', (external, self.alice['id']))
        with self.assertRaises(HTTPException):
            cal.preparation(self.db, self.alice, key)

    def test_worker_context_disappears_on_disconnect_or_folder_change_and_audio_can_still_be_saved(self):
        self.sync([{'items': [event()]}])
        key = self.rows()[0]['id']
        meeting = self.api.create_meeting(self.api.Meeting(title='Real audio', calendar_event_id=key), (self.db, self.alice))['id']
        def read():
            return self.db.execute('SELECT calendar_context_for_meeting(%s) AS value', (meeting,)).fetchone()['value']
        self.assertEqual(read()['title'], 'Nimbus permisos')
        self.db.execute('UPDATE meetings SET folder_id=%s WHERE id=%s', (self.folder, meeting))
        self.assertIsNone(read())
        self.db.execute('DELETE FROM calendar_connections WHERE owner_id=%s', (self.alice['id'],))
        self.assertIsNone(read())
        second = self.api.create_meeting(self.api.Meeting(title='Disconnected', calendar_event_id=key), (self.db, self.alice))
        self.assertEqual(second['state'], 'capturing')

    def test_connect_rejects_another_google_account_and_does_not_store_their_grant(self):
        body = cal.ConnectionIn(access_token='synthetic-access-token', refresh_token='synthetic-refresh-token')
        with patch.object(cal, 'google', return_value={'email': 'someone-else@example.test', 'email_verified': True}):
            with self.assertRaises(HTTPException) as error:
                cal.connect_google(self.db, self.alice, body)
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(self.db.execute('SELECT email FROM calendar_connections').fetchone()['email'], self.alice['email'])

    def test_offline_grant_and_read_scope_are_checked_before_connection_replacement(self):
        body = cal.ConnectionIn(access_token='synthetic-access-token', refresh_token='synthetic-refresh-token')
        replies = [{'email': self.alice['email'], 'email_verified': True}, {'access_token': 'new', 'scope': 'profile'}]
        with patch.object(cal, 'google', side_effect=replies):
            with self.assertRaises(HTTPException):
                cal.connect_google(self.db, self.alice, body)
        replies[1]['scope'] = cal.SCOPE
        with patch.object(cal, 'google', side_effect=[*replies, {'items': []}]):
            self.assertTrue(cal.connect_google(self.db, self.alice, body)['connected'])


if __name__ == '__main__':
    unittest.main()
