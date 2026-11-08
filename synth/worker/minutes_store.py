"""Database side of minutes v2: build the meeting context and persist tasks/decisions."""
from __future__ import annotations

import logging
from dataclasses import asdict

from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from synth.server.db import audit
from synth.server import context_scope

from .document_context import MeetingContext, confirmed_participants


def context_still_current(original, keys, aliases, fresh, fresh_keys, fresh_aliases):
    """New context is optional; every source actually used must remain current.

    Another meeting can add terms/tasks while this one generates. Those additions
    cannot invalidate unchanged sources or renumber the in-flight P/D references.
    """
    left,right = asdict(original),asdict(fresh)
    for field in ('glossary','open_tasks','open_decisions'):
        left.pop(field);right.pop(field)
    if left != right or keys['context_scope'] != fresh_keys['context_scope']:
        return False
    if any(term not in fresh.glossary for term in original.glossary):
        return False
    if any(alias not in fresh_aliases for alias in aliases):
        return False
    for field,kind in [('open_tasks','tasks'),('open_decisions','decisions')]:
        current={str(fresh_keys[kind][item['key']]):{k:v for k,v in item.items() if k!='key'}
                 for item in getattr(fresh,field)}
        for item in getattr(original,field):
            if current.get(str(keys[kind][item['key']])) != {k:v for k,v in item.items() if k!='key'}:
                return False
    return True


def load_context(db, meeting_id, transcript: dict[str, Any], notes: str) -> tuple[MeetingContext, dict[str, Any]]:
    """Context for one meeting plus the private key map (P1 → task id) used when persisting."""
    scope = context_scope.snapshot(db,meeting_id,worker=True)
    meeting = db.execute("SELECT id,title,folder_id,owner_id,created_at FROM meetings WHERE id=%s", (meeting_id,)).fetchone()
    folder = None
    glossary: list[dict[str, Any]] = []
    open_tasks: list[dict[str, Any]] = []
    open_decisions: list[dict[str, Any]] = []
    keys: dict[str, Any] = {"tasks": {}, "decisions": {}}
    if meeting["folder_id"]:
        row = db.execute("SELECT name FROM folders WHERE id=%s", (meeting["folder_id"],)).fetchone()
        folder = row["name"] if row else None
    if has_table(db, "glossary_terms"):
        glossary = db.execute("""SELECT id::text AS id, term, kind, definition, aliases FROM glossary_terms WHERE status='aprobado'
            AND (scope='workspace' OR folder_id=%s) ORDER BY occurrences DESC, term LIMIT 80""", (meeting["folder_id"],)).fetchall()
    if meeting["folder_id"] and has_table(db, "tasks"):
        rows = db.execute("""SELECT t.id, t.title, t.owner_name FROM tasks t JOIN meetings m ON m.id=t.meeting_id
            WHERE m.folder_id=%s AND m.id<>%s AND t.status='abierta' AND m.created_at < %s
            AND t.document_version=(SELECT max(version) FROM document_versions WHERE meeting_id=t.meeting_id)
            ORDER BY m.created_at DESC, t.item_key LIMIT 30""", (meeting["folder_id"], meeting_id, meeting["created_at"])).fetchall()
        for index, row in enumerate(rows, 1):
            keys["tasks"][f"P{index}"] = row["id"]
            open_tasks.append({"key": f"P{index}", "title": row["title"], "owner": row["owner_name"]})
        rows = db.execute("""SELECT d.id, d.decision FROM decisions d JOIN meetings m ON m.id=d.meeting_id
            WHERE m.folder_id=%s AND m.id<>%s AND d.status IN ('acordada','pendiente de validar') AND m.created_at < %s
            AND d.document_version=(SELECT max(version) FROM document_versions WHERE meeting_id=d.meeting_id)
            ORDER BY m.created_at DESC, d.item_key LIMIT 30""", (meeting["folder_id"], meeting_id, meeting["created_at"])).fetchall()
        for index, row in enumerate(rows, 1):
            keys["decisions"][f"D{index}"] = row["id"]
            open_decisions.append({"key": f"D{index}", "decision": row["decision"]})
    calendar = db.execute('SELECT calendar_context_for_meeting(%s) AS content', (meeting_id,)).fetchone()['content']
    context = MeetingContext(title=meeting["title"], started_at=meeting["created_at"], folder=folder,
                             participants=confirmed_participants(transcript), glossary=tuple(dict(g) for g in glossary),
                             open_tasks=tuple(open_tasks), open_decisions=tuple(open_decisions), notes=notes or "",
                             extra={'calendar': calendar} if calendar else {})
    return context, {**keys, "owner_id": meeting["owner_id"], "folder_id": meeting["folder_id"], 'context_scope':scope}


def has_table(db, name: str) -> bool:
    return db.execute("SELECT to_regclass(%s) AS t", (name,)).fetchone()["t"] is not None


def owner_principal(owner: str | None, transcript: dict[str, Any], meeting_owner) -> Any:
    if owner == "Tú":
        return meeting_owner
    for speaker in (transcript.get("speakers") or {}).values():
        if speaker.get("status") == "confirmed" and speaker.get("name") == owner and speaker.get("employee_id"):
            try:
                return UUID(speaker["employee_id"])
            except ValueError:
                return None
    return None


def previous_edits(db, table: str, meeting_id, field: str) -> dict[str, dict[str, Any]]:
    """Rows a person edited in the previous minutes of this meeting, by their normalized text."""
    rows = db.execute(f"""SELECT * FROM {table} WHERE meeting_id=%s AND edited_by IS NOT NULL AND document_version=
        (SELECT max(document_version) FROM {table} WHERE meeting_id=%s)""", (meeting_id, meeting_id)).fetchall()
    return {" ".join(r[field].casefold().split()): r for r in rows}


def persist_items(db, meeting_id, document_version: int, content: dict[str, Any], transcript: dict[str, Any],
                  keys: dict[str, Any]) -> None:
    if content.get("schema_version") != 2 or not has_table(db, "tasks"):
        return
    # Regenerated minutes keep what a person changed on the same task or decision (status, owner, date).
    edited_tasks = previous_edits(db, "tasks", meeting_id, "title")
    edited_decisions = previous_edits(db, "decisions", meeting_id, "decision")
    for task in content["tasks"]:
        principal = owner_principal(task["owner"], transcript, keys.get("owner_id"))
        if principal is not None and not db.execute("SELECT 1 FROM principals WHERE id=%s", (principal,)).fetchone():
            principal = None
        db.execute("""INSERT INTO tasks(id,meeting_id,document_version,item_key,title,description,owner_name,owner_principal_id,
            owner_evidence,due_text,due_date,lifecycle,previous_task_id,citations,confidence)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (uuid4(), meeting_id, document_version, task["key"], task["title"], task.get("description") or "",
                    None if task["owner"] == "sin asignar" else task["owner"], principal, task["owner_evidence"],
                    task.get("due_text"), task.get("due_date"), task["lifecycle"],
                    keys["tasks"].get(task.get("previous")), Jsonb(task["citations"]), task.get("confidence")))
        kept = edited_tasks.get(" ".join(task["title"].casefold().split()))
        if kept:
            db.execute("""UPDATE tasks SET status=%s, owner_name=%s, owner_principal_id=%s, due_date=%s, edited_by=%s
                WHERE meeting_id=%s AND document_version=%s AND item_key=%s""",
                       (kept["status"], kept["owner_name"], kept["owner_principal_id"], kept["due_date"], kept["edited_by"],
                        meeting_id, document_version, task["key"]))
        previous = keys["tasks"].get(task.get("previous"))
        if task["lifecycle"] == "completada" and previous:
            # Another meeting's task closes only if nobody edited it by hand; the change is audited.
            if db.execute("UPDATE tasks SET status='hecha',updated_at=now() WHERE id=%s AND status='abierta' AND edited_by IS NULL",
                          (previous,)).rowcount:
                audit(db, None, "task.completed_by_minutes", meeting_id, {"task_id": str(previous)})
    for decision in content["decisions"]:
        db.execute("""INSERT INTO decisions(id,meeting_id,document_version,item_key,decision,reason,alternatives,status,affects,
            decided_by,citations) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (uuid4(), meeting_id, document_version, decision["key"], decision["decision"], decision.get("reason"),
                    Jsonb(decision.get("alternatives") or []), decision["status"], decision.get("affects"),
                    decision.get("decided_by"), Jsonb(decision["citations"])))
        kept = edited_decisions.get(" ".join(decision["decision"].casefold().split()))
        if kept:
            db.execute("""UPDATE decisions SET status=%s, reason=%s, edited_by=%s
                WHERE meeting_id=%s AND document_version=%s AND item_key=%s""",
                       (kept["status"], kept["reason"], kept["edited_by"], meeting_id, document_version, decision["key"]))


def store_corrected(db, meeting_id, raw_version: int, corrected: dict[str, Any]) -> int | None:
    """Insert the corrected transcript as the next version if the raw one is still the latest; returns the version.

    None when a newer transcript arrived meanwhile: minutes written from the corrected text must not be pinned to the
    raw version, whose text differs; the newer version has its own minutes job."""
    import hashlib
    import json
    from synth.contracts.transcript import Transcript
    latest = db.execute("SELECT max(version) AS v FROM transcript_versions WHERE meeting_id=%s", (meeting_id,)).fetchone()["v"]
    if latest != raw_version:
        return None
    content = Transcript.model_validate(corrected).model_dump()
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    db.execute("""INSERT INTO transcript_versions(meeting_id,version,content,content_hash,model_fingerprint,search_text)
        VALUES (%s,%s,%s,%s,%s,%s)""", (meeting_id, raw_version + 1, Jsonb(content), digest,
                                        "glossary-corrected:" + content["model_fingerprint"],
                                        " ".join(s["text"] for s in content["segments"])))
    return raw_version + 1


def alias_terms(db, meeting_id) -> list[dict[str, Any]]:
    """Every approved term with a «sounds like» alias that this meeting's readers share: the company glossary plus the
    meeting's folder. Unlike the minutes context there is no top-N cut: matching aliases costs no model tokens."""
    if not has_table(db, "glossary_terms"):
        return []
    return [dict(r) for r in db.execute("""SELECT g.term, g.aliases, g.alias_feedback FROM glossary_terms g JOIN meetings m ON m.id=%s
        WHERE g.status='aprobado' AND g.aliases <> '{}' AND (g.scope='workspace' OR g.folder_id=m.folder_id)""", (meeting_id,)).fetchall()]


LOG = logging.getLogger("synth.worker")
LEARN_AFTER = 2  # a misspelling heard this many times next to a term becomes its alias


def learn_aliases(db, folder_id) -> int:
    """Promote phonetic proposals («sounds like Valdivar») that recur into aliases of their term, so later meetings get
    them corrected without anyone registering them. The term may be the company's: an alias is a spelling, not an
    excerpt of the meeting, so it may leave the folder (the proposal and its examples stay in it). Only invented spellings: a real word or name («Faro», «Claudio»)
    stays a proposal for a person, because as an alias it would only suggest and could mean something else."""
    from synth.server.glossary import _clean_aliases
    from .glossary_correction import alias_mode
    if folder_id is None or not has_table(db, "glossary_terms"):
        return 0
    learned = 0
    rows = db.execute("""SELECT p.id, p.term, p.occurrences, t.id AS target, t.aliases FROM glossary_terms p
        JOIN glossary_terms t ON t.id::text = substring(p.definition FROM '^Posible alias de ([0-9a-f-]{36})$')
        WHERE p.status='propuesto' AND p.origin='fonetico' AND p.folder_id=%s AND p.occurrences >= %s
          AND t.status='aprobado' AND (t.scope='workspace' OR t.folder_id=p.folder_id)""",
                      (folder_id, LEARN_AFTER)).fetchall()
    for row in rows:
        if alias_mode(row["term"])[0] != "corrige":
            continue
        db.execute("UPDATE glossary_terms SET aliases=%s, occurrences=occurrences+%s, updated_at=now() WHERE id=%s",
                   (_clean_aliases(list(row["aliases"]) + [row["term"]]), row["occurrences"], row["target"]))
        db.execute("UPDATE glossary_terms SET status='descartado', updated_at=now() WHERE id=%s", (row["id"],))
        audit(db, None, "glossary.alias_learned", row["target"], {"from": str(row["id"]), "occurrences": row["occurrences"]})
        learned += 1
    if learned:
        LOG.info("glossary_aliases_learned count=%s", learned)
    return learned


def store_candidates(db, proposals: list[dict[str, Any]], folder_id) -> int:
    """Glossary entries stay in the meeting's folder: their names and examples are only readable by its members.

    A private meeting has no audience beyond its owner, so it adds nothing to the shared glossary. Grounded entities
    with a definition are added approved; the rest are proposals."""
    from synth.server.glossary import upsert_candidate, upsert_entity
    if folder_id is None or not has_table(db, "glossary_terms"):
        return 0
    for proposal in proposals:
        if proposal.get("auto"):
            upsert_entity(db, proposal["term"], proposal["kind"], proposal["definition"], folder_id,
                          proposal.get("examples", []), proposal.get("occurrences", 1))
        else:
            upsert_candidate(db, None, proposal["term"], proposal["origin"], folder_id, proposal.get("kind", "concepto"),
                             proposal.get("examples", []), proposal.get("occurrences", 1), proposal.get("alias_of"))
    return len(proposals)
