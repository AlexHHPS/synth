"""One reader for every minutes version: v1 (flat sections) is shown in the v2 shape.

The stored document is never rewritten; this is a read-time view for the app.
"""
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from .db import authorize_meeting


def _citations(item: dict[str, Any]) -> list[str]:
    raw = item.get("citations") or item.get("evidence") or []
    # Stored minutes are historical data: a malformed citation is skipped, never a 500.
    ids = [c.get("segment_id") if isinstance(c, dict) else c for c in raw]
    return [i for i in ids if isinstance(i, str) and i]


def as_v2(content: dict[str, Any]) -> dict[str, Any]:
    if content.get("schema_version") == 2:
        return content
    points = [{"text": i["text"], "citations": _citations(i)} for i in content.get("summary", [])]
    tasks = [{"key": f"t{n}", "title": a["text"], "description": "", "owner": a.get("owner") or "sin asignar",
              "owner_evidence": "explicito" if a.get("owner") else "ninguno", "due_text": None, "due_date": a.get("due_date"),
              "lifecycle": "nueva", "previous": None, "citations": _citations(a), "topic": "Resumen"}
             for n, a in enumerate(content.get("actions", []), 1)]
    decisions = [{"key": f"d{n}", "decision": d["text"], "reason": None, "alternatives": [], "status": "acordada",
                  "affects": None, "decided_by": None, "citations": _citations(d), "topic": "Resumen"}
                 for n, d in enumerate(content.get("decisions", []), 1)]
    # The tag names the shape returned (v2); `converted_from` keeps the stored version visible.
    return {"schema_version": 2, "converted_from": 1, "language": content.get("language", "es"), "meeting_type": None,
            "tldr": points[0]["text"] if points else "",
            "topics": [{"title": "Resumen", "context": "", "discussion": "", "outcome": "", "points": points}] if points else [],
            "tasks": tasks, "decisions": decisions,
            "open_questions": [{"text": q["text"], "citations": _citations(q)} for q in content.get("open_questions", [])]}


def register(app, session_dependency) -> None:
    @app.get("/v1/meetings/{meeting_id}/minutes")
    def minutes(meeting_id: UUID, context: session_dependency, version: int | None = None):
        db, actor = context
        authorize_meeting(db, actor, meeting_id)
        row = db.execute("""SELECT version,transcript_version,content,model_fingerprint,created_at FROM document_versions
            WHERE meeting_id=%s AND ((%s::int IS NULL AND transcript_version=(SELECT max(version) FROM transcript_versions WHERE meeting_id=%s))
            OR version=%s) ORDER BY version DESC LIMIT 1""", (meeting_id, version, meeting_id, version)).fetchone()
        if not row:
            raise HTTPException(404, "Todavía no hay un acta para la transcripción actual.")
        return {"version": row["version"], "transcript_version": row["transcript_version"], "created_at": row["created_at"],
                "minutes": as_v2(row["content"])}
