"""Durable drafts owned by Synth, fenced by the existing job lease.

Reading or reconnecting never starts inference. Raw model replies are kept apart
from the public event feed, before parsing, and reused after a worker crash.
"""
import hashlib
import json
from uuid import uuid4
from psycopg.types.json import Jsonb
from synth.server import context_scope

MAX_BYTES = 16 * 1024 * 1024
EVENT_KINDS = {"started", "topic", "delta", "complete", "failed"}


def fence(db, job, worker):
    db.execute("SELECT id FROM meetings WHERE id=%s FOR UPDATE", (job["meeting_id"],))
    current = db.execute("""SELECT id FROM jobs WHERE id=%s AND meeting_id=%s AND state='running'
        AND lease_owner=%s AND attempts=%s AND lease_until>clock_timestamp()
        AND (payload->>'transcript_version')::int=(SELECT max(version) FROM transcript_versions WHERE meeting_id=%s)
        FOR UPDATE""", (job["id"], job["meeting_id"], worker, job["attempts"], job["meeting_id"])).fetchone()
    if not current:
        raise ValueError("generation_fenced")
    generation = db.execute('SELECT context_scope FROM document_generations WHERE job_id=%s AND attempt=%s',
                            (job['id'],job['attempts'])).fetchone()
    if generation:
        context_scope.assert_current(db,job['meeting_id'],generation['context_scope'],worker=True)


def begin(db, job, worker):
    fence(db, job, worker)
    scope = context_scope.snapshot(db,job['meeting_id'],worker=True)
    db.execute("""UPDATE document_generations SET state='superseded',updated_at=now()
        WHERE job_id=%s AND attempt<>%s AND state='running'""", (job["id"], job["attempts"]))
    row = db.execute("""INSERT INTO document_generations(id,job_id,meeting_id,transcript_version,attempt,context_scope)
        VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (job_id,attempt) DO UPDATE SET job_id=EXCLUDED.job_id
        RETURNING id""", (uuid4(), job["id"], job["meeting_id"], job["payload"]["transcript_version"], job["attempts"],Jsonb(scope))).fetchone()
    append(db, row["id"], job, worker, "started", "started", {"transcript_version": job["payload"]["transcript_version"]})
    return row["id"]


def append(db, generation, job, worker, key, kind, content):
    if kind not in EVENT_KINDS or not isinstance(key, str) or not 1 <= len(key) <= 200:
        raise ValueError("generation_event_invalid")
    size = len(json.dumps(content, ensure_ascii=False).encode())
    if size > 60_000:
        raise ValueError("generation_event_too_large")
    fence(db, job, worker)
    return _append(db, generation, job, key, kind, content, size)


def _append(db, generation, job, key, kind, content, size):
    row = db.execute("""SELECT id,state,last_sequence,bytes FROM document_generations
        WHERE id=%s AND job_id=%s AND attempt=%s FOR UPDATE""", (generation, job["id"], job["attempts"])).fetchone()
    if not row:
        raise ValueError("generation_fenced")
    old = db.execute("""SELECT sequence,kind,content FROM document_generation_events
        WHERE generation_id=%s AND event_key=%s""", (generation, key)).fetchone()
    if old:
        if old["kind"] != kind or old["content"] != content:
            raise ValueError("generation_event_conflict")
        return old["sequence"]
    if row["state"] != "running":
        raise ValueError("generation_closed")
    if row["bytes"] + size > MAX_BYTES:
        raise ValueError("generation_too_large")
    sequence = row["last_sequence"] + 1
    db.execute("""INSERT INTO document_generation_events(generation_id,meeting_id,sequence,event_key,kind,content)
        VALUES (%s,%s,%s,%s,%s,%s)""", (generation, job["meeting_id"], sequence, key, kind, Jsonb(content)))
    db.execute("""UPDATE document_generations SET last_sequence=%s,bytes=bytes+%s,updated_at=now()
        WHERE id=%s""", (sequence, size, generation))
    return sequence


def close(db, generation, job, state, document_version=None, transcript_version=None):
    """Call in the publication/failure transaction, after the job's fenced finish.

    An alias correction can produce a new transcript during publication; the
    result pins that version, while the generation retains its original input.
    """
    expected = "succeeded" if state == "complete" else "failed"
    row = db.execute("SELECT state,attempts FROM jobs WHERE id=%s FOR UPDATE", (job["id"],)).fetchone()
    if not row or row["state"] != expected or row["attempts"] != job["attempts"]:
        raise ValueError("generation_fenced")
    if state == "complete" and not db.execute("""SELECT 1 FROM document_versions
            WHERE meeting_id=%s AND version=%s AND transcript_version=%s""",
            (job["meeting_id"], document_version, transcript_version)).fetchone():
        raise ValueError("generation_document_missing")
    if state == 'complete':
        bound = db.execute('SELECT context_scope FROM document_generations WHERE id=%s', (generation,)).fetchone()
        context_scope.assert_current(db,job['meeting_id'],bound['context_scope'] if bound else None,worker=True)
    content = {"document_version": document_version, "transcript_version": transcript_version} if state == "complete" else {}
    _append(db, generation, job, state, state, content, len(json.dumps(content).encode()))
    db.execute("""UPDATE document_generations SET state=%s,result_transcript_version=%s,document_version=%s,
        updated_at=now() WHERE id=%s""", (state, transcript_version, document_version, generation))


def response_key(messages, tokens, route, *, context_binding=None):
    return hashlib.sha256(json.dumps({"messages": messages, "max_tokens": tokens, "route": route},
                                     ensure_ascii=False, sort_keys=True).encode() +
                          (context_binding or '').encode()).hexdigest()


def cached_response(db, job, key):
    row = db.execute("SELECT response FROM document_model_responses WHERE job_id=%s AND request_hash=%s",
                     (job["id"], key)).fetchone()
    return row["response"] if row else None


def save_response(db, job, worker, key, response):
    fence(db, job, worker)
    db.execute("""INSERT INTO document_model_responses(job_id,request_hash,response)
        VALUES (%s,%s,%s) ON CONFLICT (job_id,request_hash) DO NOTHING""", (job["id"], key, Jsonb(response)))
