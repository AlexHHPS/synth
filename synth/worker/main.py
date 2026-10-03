"""One-lane leased document worker; successful document publication is atomic."""
import hashlib
import json
import logging
import os
import signal
import threading
import time
from uuid import uuid4

from psycopg.types.json import Jsonb
from synth.server.db import audit, connect
from .document import generate
from .queue import checkpoint, claim, finish, heartbeat

LOG = logging.getLogger("synth.worker")
STOP = threading.Event()
WORKER = str(uuid4())


def run(job):
    payload, jid, attempt = job["payload"], job["id"], job["attempts"]
    alive, lost = threading.Event(), threading.Event()

    def renew():
        while not alive.wait(20):
            try:
                with connect() as db:
                    if not heartbeat(db, jid, WORKER, attempt, 300):
                        lost.set()
                        return
            except Exception:
                lost.set()
                return

    def save(value):
        if lost.is_set():
            raise ValueError("job_lease_lost")
        with connect() as db:
            if not checkpoint(db, jid, WORKER, attempt, value):
                raise ValueError("job_lease_lost")

    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        with connect() as db:
            transcript = db.execute("SELECT content FROM transcript_versions WHERE meeting_id=%s AND version=%s", (job["meeting_id"], payload["transcript_version"])).fetchone()["content"]
        content, markdown, responses = generate(transcript, payload["human_notes"], job["checkpoint"].get("responses"), save)
        if lost.is_set():
            raise ValueError("job_lease_lost")
        with connect() as db:
            # Lock meeting then fence the job, matching upload/retry/cancel ordering.
            db.execute("SELECT id FROM meetings WHERE id=%s FOR UPDATE", (job["meeting_id"],))
            if not finish(db, jid, WORKER, attempt):
                raise ValueError("job_lease_lost")
            version = db.execute("SELECT COALESCE(max(version),0)+1 AS version FROM document_versions WHERE meeting_id=%s", (job["meeting_id"],)).fetchone()["version"]
            hashed = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            models = sorted({r["effective_model"] for r in responses})
            db.execute("INSERT INTO document_versions(meeting_id,version,transcript_version,content,markdown,content_hash,model_fingerprint) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (job["meeting_id"], version, payload["transcript_version"], Jsonb(content), markdown, hashed, os.environ.get("OMNIROUTE_COMBO", "local-combo") + ":" + ",".join(models)))
            # A completed old version must not claim a newer pending transcript is ready.
            db.execute("""UPDATE meetings SET state=CASE WHEN
                (SELECT max(version) FROM transcript_versions WHERE meeting_id=%s)=%s
                THEN 'ready' ELSE state END,revision=revision+1,updated_at=now() WHERE id=%s""",
                (job["meeting_id"], payload["transcript_version"], job["meeting_id"]))
            audit(db, None, "document.publish", job["meeting_id"], {"version": version, "job_id": str(jid), "models": models})
        LOG.info("document_published job=%s", jid)
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and str(error).replace("_", "").isalnum() and len(str(error)) < 80 else "document_processing_failed"
        with connect() as db:
            db.execute("SELECT id FROM meetings WHERE id=%s FOR UPDATE", (job["meeting_id"],))
            if finish(db, jid, WORKER, attempt, code):
                db.execute("""UPDATE meetings SET state='failed',updated_at=now() WHERE id=%s
                    AND (SELECT max(version) FROM transcript_versions WHERE meeting_id=%s)=%s""",
                    (job["meeting_id"], job["meeting_id"], payload["transcript_version"]))
                audit(db, None, "document.fail", job["meeting_id"], {"job_id": str(jid), "error_code": code})
        LOG.warning("document_failed job=%s code=%s", jid, code)
    finally:
        alive.set()
        thread.join(timeout=6)


def main():
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    while not STOP.is_set():
        try:
            with connect() as db:
                job = claim(db, WORKER, 300)
            if job:
                run(job)
            else:
                STOP.wait(1)
        except Exception:
            LOG.warning("database_unavailable")
            STOP.wait(3)


if __name__ == "__main__":
    main()
