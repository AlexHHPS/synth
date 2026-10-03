"""Run inside the Synth API container against its real PostgreSQL, then roll back."""
from uuid import UUID, uuid4
from synth.server.db import connect
from synth.worker.queue import cancel, checkpoint, claim, finish, heartbeat, submit


def main():
    with connect() as db:
        mid = uuid4()
        owner = UUID("00000000-0000-4000-8000-000000000001")
        db.execute("INSERT INTO meetings(id,owner_id,title,state) VALUES (%s,%s,'Prueba de cola','queued')", (mid, owner))
        jid, created = submit(db, mid, "document", str(uuid4()), {"version": 1})
        assert created
        key = db.execute("SELECT idempotency_key FROM jobs WHERE id=%s", (jid,)).fetchone()["idempotency_key"]
        assert submit(db, mid, "document", key, {"version": 1}) == (jid, False)
        try:
            submit(db, mid, "document", key, {"version": 2})
        except ValueError as error:
            assert str(error) == "idempotency_conflict"
        else:
            raise AssertionError("conflicting duplicate accepted")
        first = claim(db, "worker-1")
        assert first["id"] == jid and first["attempts"] == 1
        assert claim(db, "worker-2") is None
        assert checkpoint(db, jid, "worker-1", 1, {"stage": "validated"})
        assert heartbeat(db, jid, "worker-1", 1)
        db.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (jid,))
        second = claim(db, "worker-2")
        assert second["id"] == jid and second["attempts"] == 2
        assert second["checkpoint"] == {"stage": "validated"}
        assert not checkpoint(db, jid, "worker-1", 1, {"stage": "stale"})
        assert not finish(db, jid, "worker-1", 1)
        assert cancel(db, jid)
        assert not finish(db, jid, "worker-2", 2)
        assert claim(db, "worker-3") is None
        db.rollback()
    print("QUEUE_WALK_OK: duplicate, conflict, single lane, heartbeat, recovery, checkpoint, fencing, cancellation")


main()
