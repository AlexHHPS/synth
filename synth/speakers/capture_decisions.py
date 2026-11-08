"""Durable human decisions, scoped to one capture and exact acoustic evidence."""
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import UUID


def evidence_hash(ids):
    if (not isinstance(ids, list) or len(ids) != 3
            or any(not isinstance(i, str) or len(i) != 64 or any(c not in '0123456789abcdef' for c in i) for i in ids)):
        raise ValueError('voice_confirmation_evidence_invalid')
    if len(set(ids)) != 3:
        raise ValueError('voice_confirmation_evidence_invalid')
    return hashlib.sha256(json.dumps(sorted(ids), separators=(',', ':')).encode()).hexdigest()


class CaptureDecisions:
    def __init__(self, state, capture_id, owner):
        self.capture_id, self.owner = str(UUID(capture_id)), owner
        path = Path(state)/'incremental'/self.capture_id/'voice-decisions.sqlite3'
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path != path.resolve(strict=False):
            raise ValueError('voice_capture_path_invalid')
        self.db = sqlite3.connect(path, timeout=5)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS capture(id TEXT PRIMARY KEY, owner TEXT NOT NULL, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS current(group_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
                evidence_hash TEXT NOT NULL, decision_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sealed(id INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
        ''')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO capture VALUES (?,?,0)', (self.capture_id, owner))
        row = self.db.execute('SELECT * FROM capture').fetchone()
        if row['id'] != self.capture_id or row['owner'] != owner:
            self.db.close()
            raise ValueError('voice_capture_owner_mismatch')

    def load(self):
        # One transaction: a concurrent confirmation cannot mix revision/rows.
        with self.db:
            self.db.execute('BEGIN')
            revision = self.db.execute('SELECT revision FROM capture').fetchone()[0]
            rows = {r['group_id']: dict(r) for r in self.db.execute('SELECT * FROM current')}
        return revision, rows

    def apply(self, body, guard=lambda: None):
        if (not isinstance(body, dict) or set(body) != {'action', 'group_id', 'profile_id', 'evidence_ids',
                'expected_revision', 'decision_id', 'confirmed_by_user'}
                or body['action'] not in ('confirm', 'clear') or body['confirmed_by_user'] is not True
                or not isinstance(body['group_id'], str) or not 1 <= len(body['group_id']) <= 200
                or type(body['expected_revision']) is not int or body['expected_revision'] < 0):
            raise ValueError('voice_confirmation_invalid')
        identifier = str(UUID(body['decision_id']))
        digest = evidence_hash(body['evidence_ids'])
        if body['action'] == 'confirm':
            str(UUID(body['profile_id']))
        elif body['profile_id'] is not None:
            raise ValueError('voice_confirmation_invalid')
        encoded = json.dumps(body, sort_keys=True, separators=(',', ':'))
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            previous = self.db.execute('SELECT body FROM decisions WHERE id=?', (identifier,)).fetchone()
            if previous:
                if previous['body'] != encoded:
                    raise ValueError('voice_confirmation_id_reused')
                return
            if self.db.execute('SELECT 1 FROM sealed').fetchone():
                raise ValueError('voice_capture_finalized')
            revision = self.db.execute('SELECT revision FROM capture').fetchone()[0]
            if revision != body['expected_revision']:
                raise ValueError('voice_confirmation_revision_changed')
            guard()
            if body['action'] == 'confirm':
                self.db.execute('INSERT OR REPLACE INTO current VALUES (?,?,?,?)',
                    (body['group_id'], body['profile_id'], digest, identifier))
            else:
                self.db.execute('DELETE FROM current WHERE group_id=?', (body['group_id'],))
            self.db.execute('INSERT INTO decisions VALUES (?,?)', (identifier, encoded))
            self.db.execute('UPDATE capture SET revision=revision+1')

    def seal(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            revision = self.db.execute('SELECT revision FROM capture').fetchone()[0]
            self.db.execute('INSERT OR IGNORE INTO sealed VALUES (1,?)', (revision,))
            rows = {r['group_id']: dict(r) for r in self.db.execute('SELECT * FROM current')}
        return revision, rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()
