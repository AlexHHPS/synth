"""Tasks, decisions and «Regenerar acta». Folder permissions apply; no session ⇒ 401.

Rows belong to a document version; lists show the latest version of each meeting.
Edits record who changed them; a regenerated document starts a new version.
"""
import datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from synth.worker.queue import submit
from .db import audit, authorize_meeting

TASK_FIELDS = "t.id,t.meeting_id,t.item_key,t.title,t.description,t.owner_name,t.owner_principal_id,t.owner_evidence," \
              "t.due_text,t.due_date,t.status,t.lifecycle,t.previous_task_id,t.citations,t.confidence,t.updated_at"
DECISION_FIELDS = "d.id,d.meeting_id,d.item_key,d.decision,d.reason,d.alternatives,d.status,d.affects,d.decided_by," \
                  "d.supersedes_id,d.citations,d.updated_at"
LATEST = "{a}.document_version=(SELECT max(version) FROM document_versions WHERE meeting_id={a}.meeting_id)"


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["abierta", "hecha", "descartada"] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=500)
    owner_name: str | None = Field(default=None, min_length=1, max_length=200)
    owner_principal_id: UUID | None = None
    due_date: datetime.date | None = None


class DecisionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["acordada", "pendiente de validar", "en desacuerdo", "aparcada"] | None = None
    decision: str | None = Field(default=None, min_length=1, max_length=1000)
    reason: str | None = Field(default=None, max_length=1000)


def _require_user(actor):
    if actor["kind"] != "user":
        raise HTTPException(403, "Solo una persona puede editar tareas y decisiones.")


def _update(db, actor, table, row_id, values: dict, fields: str):
    alias = table[0]
    row = db.execute(f"SELECT meeting_id FROM {table} WHERE id=%s", (row_id,)).fetchone()
    if not row:
        raise HTTPException(404, "No está disponible.")
    authorize_meeting(db, actor, row["meeting_id"], True)
    if not values:
        raise HTTPException(422, "No hay cambios.")
    assignments = ", ".join(f"{column}=%s" for column in values)
    db.execute(f"UPDATE {table} SET {assignments}, edited_by=%s, updated_at=now() WHERE id=%s",
               (*values.values(), actor["id"], row_id))
    audit(db, actor["id"], f"{table[:-1]}.update", row["meeting_id"], {"id": str(row_id), "fields": sorted(values)})
    return db.execute(f"SELECT {fields} FROM {table} {alias} WHERE {alias}.id=%s", (row_id,)).fetchone()


def register(app, session_dependency) -> None:
    @app.get("/v1/meetings/{meeting_id}/tasks")
    def meeting_tasks(meeting_id: UUID, context: session_dependency):  # slop-ok: route handler, registered by its decorator
        db, actor = context
        authorize_meeting(db, actor, meeting_id)
        rows = db.execute(f"SELECT {TASK_FIELDS} FROM tasks t WHERE t.meeting_id=%s AND {LATEST.format(a='t')} ORDER BY t.item_key",
                          (meeting_id,)).fetchall()
        return {"items": rows}

    @app.get("/v1/meetings/{meeting_id}/decisions")
    def meeting_decisions(meeting_id: UUID, context: session_dependency):  # slop-ok: route handler, registered by its decorator
        db, actor = context
        authorize_meeting(db, actor, meeting_id)
        rows = db.execute(f"SELECT {DECISION_FIELDS} FROM decisions d WHERE d.meeting_id=%s AND {LATEST.format(a='d')} ORDER BY d.item_key",
                          (meeting_id,)).fetchall()
        return {"items": rows}

    @app.get("/v1/tasks")
    def my_tasks(context: session_dependency, status: Literal["abierta", "hecha", "descartada"] = "abierta"):  # slop-ok: route handler, registered by its decorator
        """«Mis tareas»: tasks assigned to me across meetings I can still see."""
        db, actor = context
        _require_user(actor)
        rows = db.execute(f"""SELECT {TASK_FIELDS}, m.title AS meeting_title, m.created_at AS meeting_date FROM tasks t
            JOIN meetings m ON m.id=t.meeting_id
            WHERE t.owner_principal_id=%s AND t.status=%s AND {LATEST.format(a='t')}
            AND (m.owner_id=%s OR EXISTS (SELECT 1 FROM folder_memberships f WHERE f.folder_id=m.folder_id AND f.principal_id=%s)
                 OR EXISTS (SELECT 1 FROM folders f WHERE f.id=m.folder_id AND f.owner_id=%s))
            ORDER BY t.due_date NULLS LAST, m.created_at DESC LIMIT 200""",
                          (actor["id"], status, actor["id"], actor["id"], actor["id"])).fetchall()
        return {"items": rows}

    @app.patch("/v1/tasks/{task_id}")
    def update_task(task_id: UUID, body: TaskUpdate, context: session_dependency):  # slop-ok: route handler, registered by its decorator
        db, actor = context
        _require_user(actor)
        values = body.model_dump(exclude_none=True)
        if "owner_principal_id" in values and not db.execute("SELECT 1 FROM principals WHERE id=%s AND revoked_at IS NULL",
                                                             (values["owner_principal_id"],)).fetchone():
            raise HTTPException(422, "Esa persona no existe.")
        return _update(db, actor, "tasks", task_id, values, TASK_FIELDS)

    @app.patch("/v1/decisions/{decision_id}")
    def update_decision(decision_id: UUID, body: DecisionUpdate, context: session_dependency):  # slop-ok: route handler, registered by its decorator
        db, actor = context
        _require_user(actor)
        return _update(db, actor, "decisions", decision_id, body.model_dump(exclude_none=True), DECISION_FIELDS)

    @app.post("/v1/meetings/{meeting_id}/regenerate", status_code=202)
    def regenerate(meeting_id: UUID, context: session_dependency):
        """New minutes (current pipeline) for the latest transcript of an old meeting."""
        db, actor = context
        _require_user(actor)
        authorize_meeting(db, actor, meeting_id, True)
        meeting = db.execute("SELECT notes FROM meetings WHERE id=%s FOR UPDATE", (meeting_id,)).fetchone()
        row = db.execute("SELECT version,content_hash FROM transcript_versions WHERE meeting_id=%s ORDER BY version DESC LIMIT 1",
                         (meeting_id,)).fetchone()
        if not row:
            raise HTTPException(404, "La reunión todavía no tiene transcripción.")
        if db.execute("SELECT 1 FROM jobs WHERE meeting_id=%s AND kind='document' AND state IN ('queued','running')", (meeting_id,)).fetchone():
            raise HTTPException(409, "Ya se está generando un acta para esta reunión.")
        job, _ = submit(db, meeting_id, "document", f"{meeting_id}:regenerate:{uuid4()}",
                        {"transcript_version": row["version"], "transcript_hash": row["content_hash"], "human_notes": meeting["notes"]})
        db.execute("UPDATE meetings SET state='queued',updated_at=now(),revision=revision+1 WHERE id=%s", (meeting_id,))
        audit(db, actor["id"], "document.regenerate", meeting_id, {"job_id": str(job), "transcript_version": row["version"]})
        return {"job_id": job, "transcript_version": row["version"]}
