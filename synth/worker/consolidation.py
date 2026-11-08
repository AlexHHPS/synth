"""Durable aligned windows and revisioned core ownership, without model ownership.

Raw Qwen hypotheses are immutable. A decoded window survives alignment failure;
committing a neighbour resolves the previous core exactly once. A provisional
core is never silently relabelled as stable. Callers own audio hash validation,
model execution and the capture's acoustic lease.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import UUID

from .word_seams import POLICY as SEAM_POLICY, resolve_cut, validate

RATE = 48000
CORE = 12*RATE
CONTEXT = 3*RATE


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class ConsolidationJournal:
    def __init__(self, path, capture_id, policy):
        self.capture_id = str(UUID(capture_id))
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():raise ValueError('consolidation_path_invalid')
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata (capture_id TEXT PRIMARY KEY, policy TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS windows (source TEXT NOT NULL, start_frame INTEGER NOT NULL,
                descriptor TEXT NOT NULL, decoded TEXT, words TEXT, committed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(source,start_frame));
            CREATE TABLE IF NOT EXISTS source_state (source TEXT PRIMARY KEY, content TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cores (source TEXT NOT NULL, start_frame INTEGER NOT NULL,
                end_frame INTEGER NOT NULL, revision INTEGER NOT NULL, result TEXT NOT NULL,
                PRIMARY KEY(source,start_frame));
            CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY, source TEXT NOT NULL,
                start_frame INTEGER NOT NULL, end_frame INTEGER NOT NULL, ready_frame INTEGER NOT NULL,
                revision INTEGER NOT NULL, result TEXT NOT NULL,
                UNIQUE(source,start_frame,revision));''')
        implementation=hashlib.sha256(Path(__file__).read_bytes()+Path(__file__).with_name('word_seams.py').read_bytes()).hexdigest()
        value = encode({'policy': policy, 'seam': SEAM_POLICY, 'core_frames': CORE, 'context_frames': CONTEXT,
                        'implementation_sha256':implementation})
        previous = self.db.execute('SELECT * FROM metadata').fetchone()
        if previous and (previous['capture_id'] != self.capture_id or previous['policy'] != value):
            self.db.close();raise ValueError('consolidation_policy_changed')
        with self.db:self.db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)', (self.capture_id, value))

    def close(self):
        self.db.close()

    def begin(self, source, start, end, left, right, digest, closed=False):
        if (source not in ('microphone', 'system') or any(type(x) is not int for x in (start,end,left,right))
                or not 0 <= left <= start < end <= right <= 86400*RATE or start%CORE
                or end-start > CORE or start-left > CONTEXT or right-end > CONTEXT
                or not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('consolidation_window_invalid')
        if type(closed) is not bool or not closed and right-end!=CONTEXT:
            raise ValueError('consolidation_context_not_ready')
        descriptor=encode({'source':source,'start':start,'end':end,'left':left,'right':right,'sha256':digest})
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO windows(source,start_frame,descriptor) VALUES (?,?,?)', (source,start,descriptor))
        row=self.db.execute('SELECT * FROM windows WHERE source=? AND start_frame=?',(source,start)).fetchone()
        if row['descriptor']!=descriptor:raise ValueError('consolidation_audio_changed')
        return {'decoded':json.loads(row['decoded']) if row['decoded'] is not None else None,
                'words':json.loads(row['words']) if row['words'] is not None else None,'committed':bool(row['committed'])}

    def decoded(self, source, start, value):
        if not isinstance(value,dict) or not isinstance(value.get('text'),str) or len(value['text'])>20000:
            raise ValueError('consolidation_decode_invalid')
        encoded=encode(value)
        row=self.db.execute('SELECT decoded FROM windows WHERE source=? AND start_frame=?',(source,start)).fetchone()
        if row is None:raise ValueError('consolidation_window_missing')
        if row['decoded'] is not None and row['decoded']!=encoded:raise ValueError('consolidation_decode_immutable')
        with self.db:self.db.execute('UPDATE windows SET decoded=? WHERE source=? AND start_frame=?',(encoded,source,start))

    def aligned(self, source, start, words):
        row=self.db.execute('SELECT * FROM windows WHERE source=? AND start_frame=?',(source,start)).fetchone()
        if row is None or row['decoded'] is None:raise ValueError('consolidation_decode_missing')
        descriptor=json.loads(row['descriptor']);decoded=json.loads(row['decoded'])
        validate(words,(round(descriptor['left']/48),round(descriptor['right']/48)))
        if ' '.join(w['text'] for w in words).split()!=decoded['text'].split():
            raise ValueError('consolidation_alignment_text_changed')
        encoded=encode(words)
        if row['words'] is not None and row['words']!=encoded:raise ValueError('consolidation_alignment_immutable')
        with self.db:self.db.execute('UPDATE windows SET words=? WHERE source=? AND start_frame=?',(encoded,source,start))

    def _emit(self, source, start, end, words, reviews, revision, ready_frame, provisional=False):
        previous=self.db.execute('SELECT MAX(ready_frame) FROM events').fetchone()[0]
        if previous is not None and ready_frame<previous:raise ValueError('consolidation_publication_order_invalid')
        raw=self.db.execute('SELECT decoded FROM windows WHERE source=? AND start_frame=?',(source,start)).fetchone()
        presence=json.loads(raw['decoded']).get('speech_presence',{}) if raw and raw['decoded'] else {}
        if words and presence.get('has_speech') is not True:
            reviews=[*reviews,{'reason':'low_voice_evidence','start_frame':start,'end_frame':end}]
        uncertain=any(w.get('evidence_state')=='needs_review' for w in words) or bool(reviews)
        result={'text':' '.join(w['text'] for w in words),'source':source,'words':words,'reviews':reviews,
                'state':('provisional_low_voice_evidence' if uncertain else 'provisional') if provisional else 'needs_review' if uncertain else 'aligned',
                'block_id':f'{source}:{start}','revision':revision,
                'audio_start_time':min((w['start_ms']/1000 for w in words),default=start/RATE),
                'audio_end_time':max((w['end_ms']/1000 for w in words),default=end/RATE),
                'validated_through_frame':ready_frame}
        self.db.execute('INSERT OR REPLACE INTO cores VALUES (?,?,?,?,?)',(source,start,end,revision,encode(result)))
        self.db.execute('INSERT INTO events(source,start_frame,end_frame,ready_frame,revision,result) VALUES (?,?,?,?,?,?)',
                        (source,start,end,ready_frame,revision,encode(result)))

    def commit(self, source, start):
        with self.db:
            row=self.db.execute('SELECT * FROM windows WHERE source=? AND start_frame=?',(source,start)).fetchone()
            if row is None or row['words'] is None:raise ValueError('consolidation_alignment_missing')
            if row['committed']:return False
            desc=json.loads(row['descriptor']);words=json.loads(row['words'])
            previous=self.db.execute('SELECT content FROM source_state WHERE source=?',(source,)).fetchone()
            reviews=[]
            if previous:
                previous=json.loads(previous['content'])
                if previous['closed'] or previous['end']!=start:raise ValueError('consolidation_source_gap')
                merged,reviews=resolve_cut(previous['pending'],words,round(start/48))
                before=[w for w in merged if (w['start_ms']+w['end_ms'])/2 < start/48]
                pending=[w for w in merged if (w['start_ms']+w['end_ms'])/2 >= start/48]
                old_reviews=previous['reviews']+[r for r in reviews if (r['start_ms']+r['end_ms'])/2 < start/48]
                self._emit(source,previous['start'],previous['end'],before,old_reviews,2,desc['right'])
            else:
                if start!=0:raise ValueError('consolidation_source_gap')
                pending=words
            current_reviews=[r for r in reviews if (r['start_ms']+r['end_ms'])/2 >= start/48]
            # Show the observed right-context words immediately as provisional.
            # Ownership is corrected atomically with the next core; a word at
            # the cut must not disappear from live text while waiting for it.
            preview=pending
            self._emit(source,start,desc['end'],preview,current_reviews,1,desc['right'],provisional=True)
            state={'start':start,'end':desc['end'],'right':desc['right'],'pending':pending,
                   'reviews':current_reviews,'closed':False}
            self.db.execute('INSERT OR REPLACE INTO source_state VALUES (?,?)',(source,encode(state)))
            self.db.execute('UPDATE windows SET committed=1 WHERE source=? AND start_frame=?',(source,start))
        return True

    def finish(self, source, closed_frame):
        with self.db:
            row=self.db.execute('SELECT content FROM source_state WHERE source=?',(source,)).fetchone()
            if row is None:raise ValueError('consolidation_source_missing')
            state=json.loads(row['content'])
            if state['end']!=closed_frame or state['right']!=closed_frame:
                raise ValueError('consolidation_tail_unobserved')
            if state['closed']:return False
            self._emit(source,state['start'],state['end'],state['pending'],state['reviews'],2,closed_frame)
            state['closed']=True
            self.db.execute('UPDATE source_state SET content=? WHERE source=?',(encode(state),source))
        return True

    def events(self, after=0, through_frame=None):
        if type(after) is not int or after<0:raise ValueError('consolidation_cursor_invalid')
        return [{'sequence':r['sequence'],'source':r['source'],'start_frame':r['start_frame'],
                 'end_frame':r['end_frame'],'ready_frame':r['ready_frame'],'result':json.loads(r['result'])} for r in
                self.db.execute('SELECT * FROM events WHERE sequence>? AND (? IS NULL OR ready_frame<=?) ORDER BY sequence LIMIT 64',
                                (after,through_frame,through_frame))]

    def cores(self, source):
        return [dict(row)|{'result':json.loads(row['result'])} for row in self.db.execute(
            'SELECT * FROM cores WHERE source=? ORDER BY start_frame',(source,))]
