"""Provisional decisions, tasks and facts while a meeting is still being recorded.

The desktop sends the lines transcribed since its last request plus the state it
already holds; the worker returns the updated state. Nothing here is a record:
the acta is written from the full transcript when the meeting ends, and these
rows are deleted within hours. Only the person recording can read them.
"""
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from synth.worker import semantic_state

MAX_CHARS = 60_000
MAX_WAITING = 2


class Line(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9]{1,12}$")
    speaker: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=4000)


class LiveNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capture_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,100}$")
    lines: list[Line] = Field(min_length=1, max_length=300)
    state: list[dict[str, Any]] | None = Field(default=None, max_length=semantic_state.MAX_ITEMS)
    notes: str = Field(default="", max_length=4000)


def submit(db, actor, body: LiveNotes) -> dict[str, Any]:
    if actor["kind"] != "user":
        raise HTTPException(403, "Solo la persona que graba puede pedir notas en directo.")
    sources = [line.model_dump() for line in body.lines]
    if sum(len(line["text"]) for line in sources) > MAX_CHARS or len({line["id"] for line in sources}) != len(sources):
        raise HTTPException(422, "El tramo enviado no es válido.")
    if body.state is not None:
        try:
            semantic_state.validate({"items": body.state, "markdown": ""}, sources)
        except (ValueError, TypeError, KeyError) as error:
            raise HTTPException(422, "El estado anterior no corresponde a las líneas enviadas.") from error
    waiting = db.execute("""SELECT count(*) AS total FROM questions WHERE asker_id=%s AND kind='live'
        AND state IN ('queued','running')""", (actor["id"],)).fetchone()["total"]
    if waiting >= MAX_WAITING:
        raise HTTPException(429, "Ya hay una actualización en curso.")
    identifier = uuid4()
    db.execute("""INSERT INTO questions(id,asker_id,question,passages,kind) VALUES (%s,%s,%s,%s,'live')""",
               (identifier, actor["id"], "live:" + body.capture_id,
                Jsonb({"lines": sources, "state": body.state, "notes": body.notes})))
    return {"id": identifier, "state": "queued"}


def read(db, actor, identifier: UUID) -> dict[str, Any]:
    row = db.execute("""SELECT id,state,answer,error_code FROM questions
        WHERE id=%s AND asker_id=%s AND kind='live'""", (identifier, actor["id"])).fetchone()
    if not row:
        raise HTTPException(404, "La actualización no está disponible.")
    return {"id": row["id"], "state": row["state"], "items": (row["answer"] or {}).get("items"),
            "error_code": row["error_code"]}


def register(app, session_dependency) -> None:
    @app.post("/v1/live-notes", status_code=201)
    def create_live_notes(body: LiveNotes, context: session_dependency):  # slop-ok: route handler
        db, actor = context
        return submit(db, actor, body)

    @app.get("/v1/live-notes/{identifier}")
    def get_live_notes(identifier: UUID, context: session_dependency):  # slop-ok: route handler
        db, actor = context
        return read(db, actor, identifier)
