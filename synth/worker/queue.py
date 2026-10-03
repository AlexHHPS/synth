"""One inference lane with fenced leases, idempotency and explicit cancellation."""
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


def claim(db, worker, lease_seconds=120):
    # Serialize claim decisions, rather than overlapping acoustic/LLM jobs.
    db.execute("SELECT pg_advisory_xact_lock(18280002)")
    if db.execute("SELECT 1 FROM jobs WHERE state='running' AND lease_until>now() LIMIT 1").fetchone():
        return None
    candidate = db.execute("""SELECT id,meeting_id FROM jobs WHERE
        ((state='queued' AND available_at<=now()) OR (state='running' AND lease_until<=now()))
        AND (payload->>'transcript_version')::int=
            (SELECT max(version) FROM transcript_versions WHERE meeting_id=jobs.meeting_id)
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
