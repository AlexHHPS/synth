"""What of a published acta its current readers may see.

An acta lists the open tasks and decisions of the folder it was written in,
including those the meeting never touched. When the meeting is moved, the new
folder's members must not read the previous folder's untouched pending items.
Items that were discussed are part of this meeting and stay.
"""
from synth.worker.minutes import render_markdown

UNMENTIONED = "sin mencionar"


def written_for_current_folder(db, meeting_id, version):
    """True when the receipt of this document names the meeting's present folder."""
    row = db.execute("""SELECT g.context_scope->>'folder_id' AS written, m.folder_id::text AS present
        FROM meetings m LEFT JOIN LATERAL (SELECT context_scope FROM document_generations
            WHERE meeting_id=m.id AND document_version=%s AND state='complete' AND context_scope IS NOT NULL
            ORDER BY created_at DESC LIMIT 1) g ON true
        WHERE m.id=%s""", (version, meeting_id)).fetchone()
    if not row or not _has_receipt(db, meeting_id, version):
        # Documents older than receipts: the folder they used is unknown.
        return False
    return row["written"] == row["present"]


def _has_receipt(db, meeting_id, version):
    return db.execute("""SELECT 1 FROM document_generations WHERE meeting_id=%s AND document_version=%s
        AND state='complete' AND context_scope IS NOT NULL""", (meeting_id, version)).fetchone() is not None


def for_readers(db, meeting_id, row):
    """The stored document row, without another folder's untouched pending items."""
    content = row["content"]
    pending = content.get("continuity") if isinstance(content, dict) else None
    if not pending or all(item.get("status") != UNMENTIONED for item in pending):
        return row
    if written_for_current_folder(db, meeting_id, row["version"]):
        return row
    content = {**content, "continuity": [item for item in pending if item.get("status") != UNMENTIONED]}
    visible = {**row, "content": content}
    if "markdown" in row and "tldr" in content and "topics" in content:
        visible["markdown"] = render_markdown(content)
    return visible
