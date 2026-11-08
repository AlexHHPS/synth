"""Owner actions on meeting speakers: confirm, reject, rename, merge.

Each action creates a new transcript version (names live in `speakers`) and
queues a new document, so the minutes are regenerated with the resolved names.
Only the meeting owner may act. Unconfirmed suggestions never reach this API.
"""

import copy
import json
import hashlib
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from synth.contracts.transcript import Transcript

router = APIRouter()


class SpeakerAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["confirm", "reject", "rename", "merge"]
    speaker_id: str = Field(min_length=1, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    employee_id: UUID | None = None
    into: str | None = Field(default=None, min_length=1, max_length=200)
    expected_version: int = Field(ge=1)

    @model_validator(mode="after")
    def fields_for_action(self):  # slop-ok: pydantic validator, called by the model
        needs = {"confirm": ("name",), "rename": ("name",), "merge": ("into",), "reject": ()}[self.action]
        if any(getattr(self, field) is None for field in needs):
            raise ValueError("speaker_action_fields_missing")
        if self.action == "merge" and self.into == self.speaker_id:
            raise ValueError("speaker_merge_into_itself")
        return self


class SpeakerBatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    actions: list[SpeakerAction] = Field(min_length=1, max_length=50)

    @model_validator(mode='after')
    def consistent(self):
        if (any(a.action != 'confirm' for a in self.actions)
                or len({a.expected_version for a in self.actions}) != 1
                or len({a.speaker_id for a in self.actions}) != len(self.actions)):
            raise ValueError('speaker_batch_invalid')
        return self


def speaker_ids(content: dict[str, Any]) -> set[str]:
    labels = {s.get("speaker_id") for s in content["segments"]}
    labels |= {t.get("speaker_id") for t in content.get("turns") or []}
    return {label for label in labels if label}


def apply_action(content: dict[str, Any], action: SpeakerAction) -> dict[str, Any]:
    """Pure transformation of a transcript; raises ValueError for an unknown speaker."""
    result = copy.deepcopy(content)
    if action.speaker_id not in speaker_ids(result):
        raise ValueError("speaker_unknown")
    speakers = dict(result.get("speakers") or {})
    rows = result["segments"] + (result.get("turns") or [])
    employee = str(action.employee_id) if action.employee_id else None
    if action.action == "merge":
        if action.into not in speaker_ids(result):
            raise ValueError("speaker_unknown")
        for row in rows:
            if row.get("speaker_id") == action.speaker_id:
                row["speaker_id"] = action.into
                row["employee_id"] = (speakers.get(action.into) or {}).get("employee_id")
        speakers.pop(action.speaker_id, None)
    elif action.action == "reject":
        speakers[action.speaker_id] = {"status": "rejected"}
        for row in rows:
            if row.get("speaker_id") == action.speaker_id:
                row["employee_id"] = None
    else:
        status = "confirmed" if action.action == "confirm" else "renamed"
        speakers[action.speaker_id] = {"status": status, "name": action.name.strip(),
                                       "employee_id": employee if action.action == "confirm" else None}
        for row in rows:
            if row.get("speaker_id") == action.speaker_id:
                row["employee_id"] = employee if action.action == "confirm" else None
    result["speakers"] = speakers or None
    return Transcript.model_validate(result).model_dump()


def display_name(content: dict[str, Any], speaker_id: str | None, index: dict[str, int] | None = None) -> str:
    """The only way a speaker label becomes text: resolved name, else «Tú», else «Hablante N»."""
    if not speaker_id:
        return "Voz sin identificar"
    resolved = (content.get("speakers") or {}).get(speaker_id) or {}
    if resolved.get("status") in ("confirmed", "renamed") and resolved.get("name"):
        return resolved["name"]
    if speaker_id.endswith(":local"):
        return "Tú"
    order = index if index is not None else speaker_index(content)
    return f"Hablante {order.get(speaker_id, 0) + 1}"


def speaker_index(content: dict[str, Any]) -> dict[str, int]:
    order: dict[str, int] = {}
    for row in content.get("turns") or content["segments"]:
        label = row.get("speaker_id")
        if label and not label.endswith(":local") and label not in order:
            order[label] = len(order)
    return order


def register(app, session_dependency, store_version) -> None:
    """Mount the route on the API app (kept here so app.py only gains one line)."""

    @app.post('/v1/meetings/{meeting_id}/speakers/batch', status_code=201)
    def confirm_batch(meeting_id: UUID, body: SpeakerBatch, context: session_dependency):
        db, actor = context
        if actor['kind'] != 'user':
            raise HTTPException(403, 'Solo una persona puede confirmar hablantes.')
        meeting = db.execute('SELECT owner_id FROM meetings WHERE id=%s FOR UPDATE', (meeting_id,)).fetchone()
        if not meeting or meeting['owner_id'] != actor['id']:
            raise HTTPException(404, 'La reunión no está disponible.')
        encoded = json.dumps(body.model_dump(mode='json'), sort_keys=True)
        key = 'speakers-batch:'+hashlib.sha256(encoded.encode()).hexdigest()
        old = db.execute('SELECT id,payload FROM jobs WHERE idempotency_key=%s', (str(meeting_id)+':'+key,)).fetchone()
        if old:
            return {'job_id':old['id'], 'transcript_version':old['payload']['transcript_version'], 'reused':True}
        row = db.execute('SELECT version,content FROM transcript_versions WHERE meeting_id=%s ORDER BY version DESC LIMIT 1', (meeting_id,)).fetchone()
        if not row or row['version'] != body.actions[0].expected_version:
            raise HTTPException(409, 'La transcripción ha cambiado; vuelve a cargarla.')
        content = row['content']
        try:
            for action in body.actions:
                content = apply_action(content, action)
        except ValueError as error:
            raise HTTPException(422, 'Ese hablante no existe en esta transcripción.') from error
        return store_version(db, actor, meeting_id, Transcript.model_validate(content), key, 'transcript.speakers')

    @app.post("/v1/meetings/{meeting_id}/speakers", status_code=201)
    def act_on_speaker(meeting_id: UUID, body: SpeakerAction, context: session_dependency):
        db, actor = context
        if actor["kind"] != "user":
            raise HTTPException(403, "Solo una persona puede confirmar hablantes.")
        meeting = db.execute("SELECT owner_id FROM meetings WHERE id=%s FOR UPDATE", (meeting_id,)).fetchone()
        if not meeting or meeting["owner_id"] != actor["id"]:
            raise HTTPException(404, "La reunión no está disponible.")
        row = db.execute("SELECT version,content FROM transcript_versions WHERE meeting_id=%s ORDER BY version DESC LIMIT 1",
                         (meeting_id,)).fetchone()
        if not row:
            raise HTTPException(404, "La reunión todavía no tiene transcripción.")
        if row["version"] != body.expected_version:
            raise HTTPException(409, "La transcripción ha cambiado; vuelve a cargarla.")
        try:
            content = apply_action(row["content"], body)
        except ValueError as error:
            raise HTTPException(422, "Ese hablante no existe en esta transcripción.") from error
        key = "speakers:" + json.dumps(body.model_dump(mode="json"), sort_keys=True)
        return store_version(db, actor, meeting_id, Transcript.model_validate(content), key, "transcript.speakers")
