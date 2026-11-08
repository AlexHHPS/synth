"""Bounded checkpoint context under the meeting's folder audience, without inference.

Every read checks current access and source revisions. This is evidence retrieval,
not confirmation of words, identities or new agreements. Callers must obtain a
fresh snapshot before reuse/publication; the fingerprint is not an access token.
"""
import hashlib
import json

from fastapi import HTTPException

from .db import authorize_folder, authorize_meeting
from .glossary import visible_terms
from .questions import _score, retrieve, terms
from synth.worker.document_context import confirmed_participants

MAX_TERMS = 30
MAX_PASSAGES = 8


def snapshot(db, actor, meeting_id, query=""):
    if not isinstance(query, str) or len(query) > 500:
        raise HTTPException(422, "El contexto admite una consulta de hasta 500 caracteres.")
    authorize_meeting(db, actor, meeting_id)
    meeting = db.execute("SELECT id,title,folder_id,created_at,notes,revision FROM meetings WHERE id=%s", (meeting_id,)).fetchone()
    folder_id = meeting["folder_id"]
    # An owner can read their meeting after losing folder membership. That does
    # not grant access to the folder's vocabulary or historical conversations.
    folder = authorize_folder(db, actor, folder_id) if folder_id else None
    query = query.strip()
    stems = terms(query)
    glossary = visible_terms(db, actor, folder_id, "aprobado")
    if query:
        glossary = sorted((g for g in glossary if _score(" ".join([g["term"], *g["aliases"], g["definition"] or ""]), stems)),
                          key=lambda g: (-_score(" ".join([g["term"], *g["aliases"], g["definition"] or ""]), stems),
                                         g["folder_id"] != folder_id, -g["occurrences"], g["term"]))
    else:
        glossary = sorted(glossary, key=lambda g: (g["folder_id"] != folder_id, -g["occurrences"], g["term"]))
    vocabulary = [{"id": str(g["id"]), "term": g["term"], "kind": g["kind"],
                   "definition": (g["definition"] or "")[:600], "aliases": g["aliases"][:5],
                   "scope": g["scope"], "folder_id": str(g["folder_id"]) if g["folder_id"] else None,
                   "revision": g["updated_at"].isoformat(), "provenance": "approved_glossary"}
                  for g in glossary[:MAX_TERMS]]
    # Never search all the actor's private meetings when the target has no folder.
    passages = retrieve(db, actor, query, folder_id=folder_id, exclude_meeting_id=meeting_id,
                        created_before=meeting["created_at"])[:MAX_PASSAGES] if folder_id and stems else []
    for passage in passages:
        source = db.execute("SELECT content_hash FROM transcript_versions WHERE meeting_id=%s AND version=%s",
                            (passage["meeting_id"], passage["transcript_version"])).fetchone()
        passage.update(provenance="historical_transcript", content_hash=source["content_hash"],
                       evidence_id=f"{passage['meeting_id']}@{passage['transcript_version']}:{passage['turn_id']}")
        passage["before_evidence_id"] = (f"{passage['meeting_id']}@{passage['transcript_version']}:{passage['before_turn_id']}"
                                         if passage["before_turn_id"] else None)
        if passage["speaker"] == "Tú":
            passage["speaker"] = "Persona que grabó esa reunión"
    current = db.execute("SELECT version,content FROM transcript_versions WHERE meeting_id=%s ORDER BY version DESC LIMIT 1",
                         (meeting_id,)).fetchone()
    result = {"meeting_id": str(meeting_id), "folder_id": str(folder_id) if folder_id else None,
              "actor_id": str(actor["id"]), "key_id": str(actor.get("key_id") or ""),
              "meeting_revision": meeting["revision"], "transcript_version": current["version"] if current else None,
              "title": meeting["title"], "started_at": meeting["created_at"].isoformat(),
              "folder": folder["name"] if folder else None, "query": query,
              "participants_confirmed_in_this_meeting": list(confirmed_participants(current["content"])) if current else [],
              "human_notes": {"text": meeting["notes"][:2000], "revision": meeting["revision"],
                              "truncated": len(meeting["notes"]) > 2000, "provenance": "human_notes"},
              "glossary": vocabulary, "passages": passages,
              "usage": {"text_inference_calls": 0, "audio_inference_calls": 0},
              "policy": "folder_context_v1", "scope": "meeting_folder_only" if folder else "no_historical_folder",
              "requires_fresh_authorization_before_reuse": True}
    result["fingerprint"] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result
