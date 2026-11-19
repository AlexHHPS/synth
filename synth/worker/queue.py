"""Bounded document jobs with fenced leases, idempotency and cancellation."""
import os
from uuid import uuid4
from psycopg.types.json import Jsonb


def submit(db, meeting_id, kind, idempotency_key, payload):
    row = db.execute(
        """INSERT INTO jobs(id,meeting_id,kind,idempotency_key,payload,state)
        VALUES (%s,%s,%s,%s,%s,'queued') ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id""", (uuid4(), meeting_id, kind, idempotency_key, Jsonb(payload)),
    ).fetchone()
    if row:
        return row["id"], True
    old = db.execute("SELECT id,meeting_id,kind,payload FROM jobs WHERE idempotency_key=%s", (idempotency_key,)).fetchone()
    if old["meeting_id"] != meeting_id or old["kind"] != kind or old["payload"] != payload:
        raise ValueError("idempotency_conflict")
    return old["id"], False


def capacity():
    value = int(os.environ.get("SYNTH_DOCUMENT_CONCURRENCY", "2"))
    if not 1 <= value <= 8:
        raise ValueError("invalid_document_capacity")
    return value


# A job that has taken its worker down this many times is not retried again.
MAX_ATTEMPTS = 5


def reap(db):
    """Close jobs nobody will ever finish, so they stop holding their meeting.

    A running job whose lease expired is normally reclaimed. Two kinds are not:
    one for a transcript that has since been replaced (never claimable again),
    and one that has already failed MAX_ATTEMPTS times without reporting why,
    which is what a job that kills its worker looks like.
    """
    db.execute("""UPDATE jobs SET state='cancelled',lease_owner=NULL,lease_until=NULL,updated_at=now()
        WHERE kind='document' AND state='running' AND lease_until<=now()
        AND (payload->>'transcript_version')::int<
            (SELECT max(version) FROM transcript_versions WHERE meeting_id=jobs.meeting_id)""")
    exhausted = db.execute("""UPDATE jobs SET state='failed',error_code='attempts_exhausted',
        lease_owner=NULL,lease_until=NULL,updated_at=now()
        WHERE kind='document' AND state='running' AND lease_until<=now() AND attempts>=%s
        RETURNING meeting_id,payload""", (MAX_ATTEMPTS,)).fetchall()
    for job in exhausted:
        db.execute("""UPDATE meetings SET state='failed',updated_at=now() WHERE id=%s AND state IN ('queued','processing')
            AND (SELECT max(version) FROM transcript_versions WHERE meeting_id=%s)=%s""",
            (job["meeting_id"], job["meeting_id"], job["payload"].get("transcript_version")))


def claim(db, worker, lease_seconds=120, max_running=None):
    limit = capacity() if max_running is None else max_running
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
        raise ValueError("invalid_document_capacity")
    # Serialize admission across replicas; each claimed job retains its own lease.
    db.execute("SELECT pg_advisory_xact_lock(18280002)")
    active = db.execute("SELECT count(*) AS n FROM jobs WHERE state='running' AND lease_until>now()").fetchone()["n"]
    if active >= limit:
        return None
    reap(db)
    candidate = db.execute("""SELECT id,meeting_id FROM jobs WHERE
        ((state='queued' AND available_at<=now()) OR (state='running' AND lease_until<=now()))
        AND (payload->>'transcript_version')::int=
            (SELECT max(version) FROM transcript_versions WHERE meeting_id=jobs.meeting_id)
        AND NOT EXISTS (SELECT 1 FROM jobs other WHERE other.meeting_id=jobs.meeting_id
            AND other.id<>jobs.id AND other.state='running' AND other.lease_until>now())
        ORDER BY created_at,id LIMIT 1""").fetchone()
    if not candidate:
        return None
    # All combined meeting/job mutations lock the meeting first. Upload, retry,
    # claim and completion cannot deadlock or evaluate a stale version snapshot.
    db.execute('SELECT id FROM meetings WHERE id=%s FOR UPDATE', (candidate['meeting_id'],))
    job = db.execute(
        """UPDATE jobs j SET state='running',lease_owner=%s,
          lease_until=now()+(%s * interval '1 second'),attempts=attempts+1,updated_at=now()
          WHERE j.id=%s AND ((state='queued' AND available_at<=now()) OR
            (state='running' AND lease_until<=now())) AND
          (payload->>'transcript_version')::int=
            (SELECT max(version) FROM transcript_versions WHERE meeting_id=j.meeting_id)
          RETURNING j.*""", (worker, lease_seconds, candidate['id']),
    ).fetchone()
    if job:
        db.execute("""UPDATE meetings SET state='processing',updated_at=now() WHERE id=%s
            AND (SELECT max(version) FROM transcript_versions WHERE meeting_id=%s)=%s""",
            (job["meeting_id"], job["meeting_id"], job["payload"].get("transcript_version")))
    return job


def heartbeat(db, job_id, worker, attempt, lease_seconds=120):
    return db.execute(
        """UPDATE jobs SET lease_until=now()+(%s * interval '1 second'),updated_at=now()
        WHERE id=%s AND lease_owner=%s AND attempts=%s AND state='running'
        AND lease_until>now() RETURNING id""", (lease_seconds, job_id, worker, attempt),
    ).fetchone() is not None


def checkpoint(db, job_id, worker, attempt, value):
    return db.execute(
        """UPDATE jobs SET checkpoint=%s,updated_at=now()
        WHERE id=%s AND lease_owner=%s AND attempts=%s AND state='running'
        AND lease_until>now() RETURNING id""", (Jsonb(value), job_id, worker, attempt),
    ).fetchone() is not None


# A failed acta is tried again by itself before anyone has to press «Reintentar»:
# most failures are a gateway restart, a model answer that did not parse or a
# context edit, and the next attempt succeeds. Waits before attempts 2, 3 and 4.
RETRY_DELAYS_S = (30, 120, 600)


def defer(db, job_id, worker, attempt, error_code):
    """Release a failed attempt to run again later; False when no automatic attempt is left."""
    if attempt > len(RETRY_DELAYS_S):
        return False
    return db.execute(
        """UPDATE jobs SET state='queued',error_code=%s,lease_owner=NULL,lease_until=NULL,
        available_at=now()+(%s * interval '1 second'),updated_at=now()
        WHERE id=%s AND lease_owner=%s AND attempts=%s AND state='running'
        AND lease_until>now() RETURNING id""", (error_code, RETRY_DELAYS_S[attempt - 1], job_id, worker, attempt),
    ).fetchone() is not None


def finish(db, job_id, worker, attempt, error_code=None):
    return db.execute(
        """UPDATE jobs SET state=%s,error_code=%s,lease_owner=NULL,lease_until=NULL,updated_at=now()
        WHERE id=%s AND lease_owner=%s AND attempts=%s AND state='running'
        AND lease_until>now() RETURNING id""",
        ("failed" if error_code else "succeeded", error_code, job_id, worker, attempt),
    ).fetchone() is not None


def cancel(db, job_id):
    return db.execute(
        """UPDATE jobs SET state='cancelled',lease_owner=NULL,lease_until=NULL,updated_at=now()
        WHERE id=%s AND state IN ('queued','running','failed') RETURNING id""", (job_id,),
    ).fetchone() is not None
