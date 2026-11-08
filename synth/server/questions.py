"""Ask the meetings: retrieval over turns under the asker's permissions, answer with citations.

The API retrieves passages at ask time with the same authorization as search, so the worker that answers never
widens access. Only the asker reads the question; it expires after 7 days. Without passages the answer is
«No consta» and no model is called. No session ⇒ 401.
"""

import re
import time
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from synth.worker.answers import NO_EVIDENCE
from .db import audit, authorize_folder, authorize_meeting

MAX_MEETINGS = 20
MAX_PASSAGES = 12
STOP = frozenset("""a al algo como con cual cuales cuando de del donde el ella ellas ellos en entre era es esa ese eso esta
este esto fue ha han hay la las le les lo los mas me mi mis muy no nos o para pero por que quien quienes se ser si sin
sobre su sus te tu un una uno unos unas y ya qué quién cuándo dónde cómo cuál dijo dijimos hablamos reunión reuniones""".split())


class Ask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=3, max_length=500)
    folder_id: UUID | None = None
    meeting_id: UUID | None = None


def terms(question: str) -> list[str]:
    from synth.worker.phonetic import plain
    words = re.findall(r"[\wáéíóúñü]+", question.casefold())
    return sorted({plain(w)[:6] for w in words if len(w) > 2 and w not in STOP and plain(w) not in STOP})


def _score(text: str, stems: list[str]) -> int:
    from synth.worker.phonetic import plain
    words = {plain(w)[:6] for w in re.findall(r"[\wáéíóúñü]+", text.casefold())}
    return sum(1 for stem in stems if stem in words)


def _candidates(db, actor, question: str, folder_id, meeting_id, *, exclude_meeting_id=None, created_before=None) -> list[dict[str, Any]]:
    """Latest transcript of each meeting the actor may read that shares a word with the question.

    Ids only before authorization, like search: content is read after each meeting is authorized."""
    if meeting_id:
        authorize_meeting(db, actor, meeting_id)
        return db.execute("SELECT meeting_id, max(version) AS version FROM transcript_versions WHERE meeting_id=%s GROUP BY meeting_id",
                          (meeting_id,)).fetchall()
    if folder_id:
        authorize_folder(db, actor, folder_id)
    words = [w for w in re.findall(r"[\wáéíóúñü]+", question.casefold()) if len(w) > 2 and w not in STOP]
    if not words:
        return []
    rows = db.execute("""SELECT t.meeting_id, t.version,
            ts_rank(t.search_vector, websearch_to_tsquery('spanish', %s)) AS rank
        FROM transcript_versions t JOIN meetings m ON m.id=t.meeting_id
        WHERE t.search_vector @@ websearch_to_tsquery('spanish', %s)
        AND (%s::uuid IS NULL OR m.folder_id=%s)
        AND (%s::uuid IS NULL OR m.id<>%s)
        AND (%s::timestamptz IS NULL OR m.created_at<%s)
        AND t.version=(SELECT max(t2.version) FROM transcript_versions t2 WHERE t2.meeting_id=t.meeting_id)
        ORDER BY rank DESC, m.created_at DESC LIMIT 100""", (" OR ".join(words), " OR ".join(words), folder_id, folder_id,
                                                              exclude_meeting_id, exclude_meeting_id, created_before, created_before)).fetchall()
    allowed = []
    for row in rows:
        try:
            authorize_meeting(db, actor, row["meeting_id"])
        except HTTPException:
            continue
        allowed.append(row)
        if len(allowed) >= MAX_MEETINGS:
            break
    return allowed


def retrieve(db, actor, question: str, folder_id=None, meeting_id=None, *, exclude_meeting_id=None, created_before=None) -> list[dict[str, Any]]:
    """Best-matching turns (with the turn before for context) across the meetings the actor may read."""
    from synth.server.speakers import display_name, speaker_index
    from synth.worker.document_context import clock, turns_of
    stems = terms(question)
    if not stems:
        return []
    scored = []
    for row in _candidates(db, actor, question, folder_id, meeting_id,
                           exclude_meeting_id=exclude_meeting_id, created_before=created_before):
        saved = db.execute("""SELECT t.content, m.title, m.created_at FROM transcript_versions t JOIN meetings m ON m.id=t.meeting_id
            WHERE t.meeting_id=%s AND t.version=%s""", (row["meeting_id"], row["version"])).fetchone()
        content = saved["content"]
        order = speaker_index(content)
        turns = turns_of(content)
        for index, turn in enumerate(turns):
            score = _score(turn["text"], stems)
            if score:
                before = turns[index - 1]["text"][-300:] if index else ""
                scored.append((score, saved["created_at"], {
                    "meeting_id": str(row["meeting_id"]), "transcript_version": row["version"], "meeting_title": saved["title"],
                    "meeting_date": saved["created_at"].date().isoformat(), "turn_id": turn["id"],
                    "time": clock(turn.get("start_ms", 0)), "speaker": display_name(content, turn.get("speaker_id"), order),
                    "text": turn["text"][:1200], "before": before,
                    "before_turn_id": turns[index - 1]["id"] if index else None}))
    scored.sort(key=lambda item: (-item[0], -item[1].timestamp()))
    meetings = []
    for _, _, passage in scored:
        if passage["meeting_id"] not in meetings:
            meetings.append(passage["meeting_id"])
    keep = set(meetings[:MAX_MEETINGS])
    return [p for _, _, p in scored if p["meeting_id"] in keep][:MAX_PASSAGES]


def ask(db, actor, body: Ask) -> dict[str, Any]:
    # Before anything else: a question about a meeting or folder the actor cannot read is not stored at all, so it
    # cannot reveal whether that id exists (a question without search words skips retrieval and its checks).
    if body.meeting_id:
        authorize_meeting(db, actor, body.meeting_id)
    elif body.folder_id:
        authorize_folder(db, actor, body.folder_id)
    passages = retrieve(db, actor, body.question, body.folder_id, body.meeting_id)
    qid = uuid4()
    if not passages:
        answer = {"status": "no_consta", "text": NO_EVIDENCE, "citations": []}
        db.execute("""INSERT INTO questions(id,asker_id,folder_id,meeting_id,question,passages,state,answer)
            VALUES (%s,%s,%s,%s,%s,'[]','answered',%s)""", (qid, actor["id"], body.folder_id, body.meeting_id, body.question, Jsonb(answer)))
    else:
        db.execute("""INSERT INTO questions(id,asker_id,folder_id,meeting_id,question,passages)
            VALUES (%s,%s,%s,%s,%s,%s)""", (qid, actor["id"], body.folder_id, body.meeting_id, body.question, Jsonb(passages)))
    audit(db, actor["id"], "question.ask", body.meeting_id, {"question_id": str(qid), "passages": len(passages)})
    return read(db, actor, qid)


def read(db, actor, question_id) -> dict[str, Any]:
    row = db.execute("""SELECT id,question,state,answer,passages,error_code,created_at FROM questions
        WHERE id=%s AND asker_id=%s AND created_at > now() - interval '7 days'""", (question_id, actor["id"])).fetchone()
    if not row:
        raise HTTPException(404, "La pregunta no está disponible.")
    # Access can change after asking: every cited meeting is checked again on each read.
    for meeting_id in {p["meeting_id"] for p in row["passages"]}:
        try:
            authorize_meeting(db, actor, UUID(meeting_id))
        except HTTPException:
            raise HTTPException(404, "La pregunta no está disponible.") from None
    sources = {p["turn_id"] + "@" + p["meeting_id"]: p for p in row["passages"]}
    answer = row["answer"]
    if answer:
        answer = {**answer, "citations": [{**c, **{k: sources[c["turn_id"] + "@" + c["meeting_id"]][k]
                                                    for k in ("meeting_title", "meeting_date", "time", "speaker", "text")}}
                                           for c in answer.get("citations", []) if c["turn_id"] + "@" + c["meeting_id"] in sources]}
    return {"id": row["id"], "question": row["question"], "state": row["state"], "answer": answer,
            "error_code": row["error_code"], "created_at": row["created_at"]}


def wait(db_factory, actor, question_id, seconds: float = 45.0) -> dict[str, Any]:
    """For MCP: poll briefly so a client gets the answer in one call when the model is quick."""
    deadline = time.monotonic() + seconds
    while True:
        with db_factory() as db:
            result = read(db, actor, question_id)
        if result["state"] in ("answered", "failed") or time.monotonic() >= deadline:
            return result
        time.sleep(1.5)


def register(app, session_dependency) -> None:
    @app.post("/v1/questions", status_code=201)
    def create_question(body: Ask, context: session_dependency):  # slop-ok: route handler, registered by its decorator
        db, actor = context
        if body.folder_id and body.meeting_id:
            raise HTTPException(422, "Pregunta sobre una carpeta o sobre una reunión, no ambas.")
        return ask(db, actor, body)

    @app.get("/v1/questions/{question_id}")
    def get_question(question_id: UUID, context: session_dependency):  # slop-ok: route handler, registered by its decorator
        db, actor = context
        return read(db, actor, question_id)
