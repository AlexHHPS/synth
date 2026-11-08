"""Authorized, resumable reading of drafts. GET never launches or retries a job."""
import asyncio
import json
import time
from uuid import UUID

import anyio
from fastapi import HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .db import authenticate, authorize_meeting, bind_actor, connect
from . import context_scope

STREAM_SECONDS = 15
POLL_SECONDS = 0.25
TERMINAL = {"complete", "failed", "cancelled", "superseded"}


def details(db, meeting_id, generation_id):
    row = db.execute("""SELECT g.id,g.job_id,g.meeting_id,g.transcript_version,g.result_transcript_version,
        g.document_version,g.attempt,g.state,g.last_sequence,g.context_scope,j.state AS job_state,j.attempts AS job_attempt,
        (SELECT max(version) FROM transcript_versions WHERE meeting_id=g.meeting_id) AS latest_version
        FROM document_generations g JOIN jobs j ON j.id=g.job_id WHERE g.id=%s AND g.meeting_id=%s""",
        (generation_id, meeting_id)).fetchone()
    if not row:
        raise HTTPException(404, "generation_unavailable")
    try:
        context_scope.assert_current(db,meeting_id,row['context_scope'])
    except ValueError:
        raise HTTPException(409,'generation_context_changed') from None
    if (row["state"] == "superseded" or row["attempt"] != row["job_attempt"] or
            (row["result_transcript_version"] or row["transcript_version"]) != row["latest_version"]):
        raise HTTPException(409, "generation_superseded")
    result = {k: v for k, v in row.items() if k not in ("latest_version", "job_attempt", "context_scope")}
    if row["job_state"] in ("failed", "cancelled"):
        result["state"] = row["job_state"]
    return result


def current(db, actor, meeting_id):
    authorize_meeting(db, actor, meeting_id)
    row = db.execute("""SELECT g.id FROM document_generations g WHERE meeting_id=%s
        AND COALESCE(result_transcript_version,transcript_version)=
            (SELECT max(version) FROM transcript_versions WHERE meeting_id=g.meeting_id)
        ORDER BY g.created_at DESC,g.attempt DESC,g.id DESC LIMIT 1""", (meeting_id,)).fetchone()
    if not row:
        return {"generation": None}
    try:
        generation = details(db, meeting_id, row["id"])
    except HTTPException as error:
        if error.detail in ('generation_context_changed','generation_superseded'):
            return {'generation':None}
        raise
    pending = db.execute("""SELECT id,state FROM jobs WHERE meeting_id=%s AND state IN ('queued','running')
        AND (payload->>'transcript_version')::int=(SELECT max(version) FROM transcript_versions WHERE meeting_id=%s)
        ORDER BY created_at DESC,id DESC LIMIT 1""", (meeting_id, meeting_id)).fetchone()
    if pending and pending["id"] != generation["job_id"]:
        return {"generation": None, "job": pending}
    return {"generation": generation}


def batch(db, actor, meeting_id, generation_id, after=0, limit=100):
    # Authorization precedes fetching any text; RLS independently enforces it.
    authorize_meeting(db, actor, meeting_id)
    generation = details(db, meeting_id, generation_id)
    if after < 0 or after > generation["last_sequence"]:
        raise HTTPException(409, "generation_cursor_invalid")
    rows = db.execute("""SELECT sequence,kind,content FROM document_generation_events
        WHERE generation_id=%s AND meeting_id=%s AND sequence>%s ORDER BY sequence LIMIT %s""",
        (generation_id, meeting_id, after, limit + 1)).fetchall()
    return {"generation": generation, "events": rows[:limit], "more": len(rows) > limit,
            "next_cursor": rows[min(len(rows), limit) - 1]["sequence"] if rows else after}


def authorized_batch(db, token, actor_id, meeting_id, generation_id, cursor):
    actor = authenticate(db, token)
    if actor["id"] != actor_id:
        raise HTTPException(401, "generation_session_changed")
    bind_actor(db, actor)
    return batch(db, actor, meeting_id, generation_id, cursor)


async def events(token, actor_id, meeting_id, generation_id, cursor):
    db = None
    started, heartbeat = time.monotonic(), time.monotonic()
    try:
        db = await asyncio.to_thread(connect)
        db.autocommit = True  # No open snapshot/transaction while awaiting content.
        while time.monotonic() - started < STREAM_SECONDS:
            page = await asyncio.to_thread(authorized_batch, db, token, actor_id, meeting_id, generation_id, cursor)
            for event in page["events"]:
                cursor = event["sequence"]
                payload = {**event, "generation_id": str(generation_id),
                           "transcript_version": page["generation"]["transcript_version"]}
                yield f"id: {generation_id}:{cursor}\nevent: generation\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if not page["more"] and page["generation"]["state"] in TERMINAL:
                yield "event: status\ndata: " + json.dumps({"state": page["generation"]["state"], "cursor": cursor}) + "\n\n"
                return
            if page["more"]:
                continue
            if time.monotonic() - heartbeat >= 2:
                yield ": keepalive\n\n"
                heartbeat = time.monotonic()
            await asyncio.sleep(POLL_SECONDS)
    except HTTPException as error:
        yield "event: error\ndata: " + json.dumps({"code": error.detail, "status": error.status_code}) + "\n\n"
    except Exception:
        # No raw database/HTTP messages: they can contain meeting text or keys.
        yield 'event: error\ndata: {"code":"generation_stream_unavailable","status":503}\n\n'
    finally:
        if db is not None:
            with anyio.CancelScope(shield=True):
                await asyncio.to_thread(db.close)


def register(app, Session):
    @app.get("/v1/meetings/{meeting_id}/generation")
    def generation(meeting_id: UUID, context: Session):
        db, actor = context
        return current(db, actor, meeting_id)

    @app.get("/v1/meetings/{meeting_id}/generations/{generation_id}/events")
    def generation_events(meeting_id: UUID, generation_id: UUID, context: Session,
                          after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=100)):
        db, actor = context
        return batch(db, actor, meeting_id, generation_id, after, limit)

    @app.get("/v1/meetings/{meeting_id}/generations/{generation_id}/stream")
    def generation_stream(meeting_id: UUID, generation_id: UUID, request: Request, context: Session,
                          after: int = Query(default=0, ge=0)):
        db, actor = context
        batch(db, actor, meeting_id, generation_id, after, 1)
        token = request.headers.get("authorization", "").partition(" ")[2]
        return StreamingResponse(events(token, actor["id"], meeting_id, generation_id, after),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
