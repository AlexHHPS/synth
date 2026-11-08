"""Follow-up email draft: composed from the minutes, copied by a person, never sent.

Deterministic: no model call, so the draft says exactly what the minutes say (TL;DR, decisions, tasks with owner and
date, open questions). There is no recipient, no SMTP and no send route anywhere in the product.
"""

import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from .db import authorize_meeting

MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")


def _date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        day = datetime.date.fromisoformat(value)
    except ValueError:
        return None
    return f"{day.day} de {MONTHS[day.month - 1]}"


def compose(title: str, held: datetime.datetime | None, minutes: dict[str, Any]) -> dict[str, str]:
    when = f" del {held.day} de {MONTHS[held.month - 1]}" if held else ""
    lines = ["Hola:", "", f"Te paso el resumen de «{title}»{when}.", ""]
    if minutes.get("tldr"):
        lines += [minutes["tldr"], ""]
    decisions = [d for d in minutes.get("decisions", []) if d.get("status") != "aparcada"]
    if decisions:
        lines.append("Decisiones")
        lines += [f"- {d['decision']}" + (f" (motivo: {d['reason']})" if d.get("reason") else "") for d in decisions]
        lines.append("")
    tasks = [t for t in minutes.get("tasks", []) if t.get("status", "abierta") == "abierta"]
    if tasks:
        lines.append("Próximos pasos")
        for task in tasks:
            owner = task.get("owner") if task.get("owner") not in (None, "", "sin asignar") else "sin responsable"
            due = _date(task.get("due_date")) or task.get("due_text")
            lines.append(f"- {task['title']} — {owner}" + (f", {due}" if due else ""))
        lines.append("")
    questions = minutes.get("open_questions", [])
    if questions:
        lines.append("Queda pendiente")
        lines += [f"- {q['text']}" for q in questions]
        lines.append("")
    lines += ["Si algo no encaja con lo que recuerdas, dímelo y lo corrijo.", "", "Un saludo"]
    return {"subject": f"Resumen y próximos pasos: {title}", "body": "\n".join(lines)}


def register(app, session_dependency) -> None:
    @app.get("/v1/meetings/{meeting_id}/follow-up-email")
    def follow_up_email(meeting_id: UUID, context: session_dependency):  # slop-ok: route handler, registered by its decorator
        """A draft to copy into the user's own mail client. Nothing is sent."""
        from .minutes_view import as_v2
        db, actor = context
        authorize_meeting(db, actor, meeting_id)
        meeting = db.execute("SELECT title, created_at FROM meetings WHERE id=%s", (meeting_id,)).fetchone()
        row = db.execute("""SELECT content FROM document_versions WHERE meeting_id=%s
            AND transcript_version=(SELECT max(version) FROM transcript_versions WHERE meeting_id=%s)
            ORDER BY version DESC LIMIT 1""", (meeting_id, meeting_id)).fetchone()
        if not row:
            raise HTTPException(404, "Todavía no hay un acta para la transcripción actual.")
        minutes = as_v2(row["content"])
        # Edited task rows win over the generated minutes: the draft reflects what people corrected.
        edited = {t["item_key"]: t for t in db.execute(
            """SELECT item_key, title, owner_name, due_date, status FROM tasks WHERE meeting_id=%s
               AND document_version=(SELECT max(version) FROM document_versions WHERE meeting_id=%s)""",
            (meeting_id, meeting_id)).fetchall()}
        tasks = []
        for task in minutes.get("tasks", []):
            row_ = edited.get(task.get("key"))
            if row_:
                task = {**task, "title": row_["title"], "owner": row_["owner_name"] or task.get("owner"),
                        "due_date": row_["due_date"].isoformat() if row_["due_date"] else task.get("due_date"), "status": row_["status"]}
            tasks.append(task)
        return {**compose(meeting["title"], meeting["created_at"], {**minutes, "tasks": tasks}), "sent": False}
