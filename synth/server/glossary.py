"""Company glossary API: list, create, approve, merge aliases, discard, propose candidates.

Workspace terms are visible to every person; folder terms follow folder access.
Integrations (machine keys) may only read. No session ⇒ 401.
"""
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import audit, authorize_folder

# What a term is: the glossary is a taxonomy of the names the meetings use, not a list of capitalized words.
KINDS = Literal["persona", "organizacion", "producto", "proyecto", "sistema", "concepto"]
EDGE_PUNCTUATION = " \t\n¿?¡!.,;:«»\"'“”‘’()[]{}-–—…"
FIELDS = "id,scope,folder_id,term,kind,aliases,definition,status,origin,occurrences,examples,alias_feedback,created_at,updated_at"


def _clean_aliases(values: list[str]) -> list[str]:
    """Unique aliases, at most 30; a common Spanish word is never an alias, or every use of it would become a candidate."""
    from synth.worker.phonetic import COMMON, plain
    seen, out = set(), []
    for value in values:
        value = value.strip()
        if value and value.casefold() not in seen and len(value) <= 120 and plain(value) not in COMMON:
            seen.add(value.casefold())
            out.append(value)
    return out[:30]


def clean_term(term: str) -> str:
    """«¿Vale?» and «Vale» are one term: no surrounding punctuation, single spaces."""
    return " ".join(term.strip(EDGE_PUNCTUATION).split())[:120]


class TermIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term: str = Field(min_length=1, max_length=120)
    kind: KINDS | None = None
    aliases: list[str] = Field(default_factory=list, max_length=30)
    definition: str = Field(default="", max_length=500)
    folder_id: UUID | None = None

    @field_validator("aliases")
    @classmethod
    def unique_aliases(cls, value):  # slop-ok: pydantic validator, called by the model
        return _clean_aliases(value)


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term: str = Field(min_length=1, max_length=120)
    origin: Literal["correccion", "llm", "fonetico", "participante", "carpeta"]
    alias_of: UUID | None = None
    kind: KINDS = "concepto"
    folder_id: UUID | None = None
    examples: list[str] = Field(default_factory=list, max_length=5)
    occurrences: int = Field(default=1, ge=1, le=10_000)


class TermPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term: str | None = Field(default=None, min_length=1, max_length=120)
    aliases: list[str] | None = Field(default=None, max_length=30)
    definition: str | None = Field(default=None, max_length=500)
    kind: KINDS | None = None


class Merge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    into: UUID


def _person(actor):
    if actor["kind"] != "user":
        raise HTTPException(403, "Las integraciones solo pueden leer el glosario.")


def _merge(db, actor, source, target):
    """`source` (a variant) becomes an alias of `target`, with its own aliases; the variant is discarded."""
    # A folder term must not become a workspace alias that every user reads and every meeting preselects.
    if (source["scope"], source["folder_id"]) != (target["scope"], target["folder_id"]):
        raise HTTPException(422, "Solo se pueden fusionar términos del mismo ámbito.")
    aliases = _clean_aliases(list(target["aliases"]) + [source["term"]] + list(source["aliases"]))
    db.execute("UPDATE glossary_terms SET status='descartado', updated_at=now() WHERE id=%s", (source["id"],))
    row = db.execute(f"""UPDATE glossary_terms SET aliases=%s, occurrences=occurrences+%s, updated_at=now()
        WHERE id=%s RETURNING {FIELDS}""", (aliases, source["occurrences"], target["id"])).fetchone()
    audit(db, actor["id"], "glossary.merge", target["id"], {"from": str(source["id"])})
    return row


def _term(db, actor, term_id, write=False):
    row = db.execute(f"SELECT {FIELDS} FROM glossary_terms WHERE id=%s", (term_id,)).fetchone()
    if not row:
        raise HTTPException(404, "El término no está disponible.")
    if row["folder_id"]:
        authorize_folder(db, actor, row["folder_id"], write)
    return row


def visible_terms(db, actor, folder_id=None, status=None):
    clauses, params = ["(scope='workspace'"], []
    if folder_id:
        authorize_folder(db, actor, folder_id)
        clauses[0] += " OR folder_id=%s"
        params.append(folder_id)
    clauses[0] += ")"
    if actor["kind"] == "machine":
        clauses.append("status='aprobado'")
    if status:
        clauses.append("status=%s")
        params.append(status)
    return db.execute(f"SELECT {FIELDS} FROM glossary_terms WHERE {' AND '.join(clauses)} "
                      "ORDER BY status, occurrences DESC, lower(term) LIMIT 500", params).fetchall()


def same_term(db, folder_id, term):
    """The term of this scope that `term` is a spelling of: same letters once punctuation, accents and case are gone,
    either its name or one of its aliases. Discarded terms count too, so a deleted term is not proposed again."""
    from synth.worker.phonetic import plain
    wanted = plain(clean_term(term))
    rows = db.execute(f"SELECT {FIELDS} FROM glossary_terms WHERE scope=%s AND folder_id IS NOT DISTINCT FROM %s",
                      ("folder" if folder_id else "workspace", folder_id)).fetchall()
    matches = [r for r in rows if wanted and (plain(r["term"]) == wanted or wanted in {plain(a) for a in r["aliases"]})]
    return min(matches, key=lambda r: ("aprobado", "propuesto", "descartado").index(r["status"])) if matches else None


def plain_key(text: str) -> str:
    from synth.worker.phonetic import plain
    return plain(clean_term(text))


def _sounds_like(db, folder_id, term, threshold=0.9):
    """The approved term of the same scope (the folder, or the workspace when there is none) that `term` is a
    mishearing of, if one is very close."""
    from synth.worker.phonetic import COMMON, similarity
    if plain_key(term) in COMMON:
        return None
    rows = db.execute(f"""SELECT {FIELDS} FROM glossary_terms WHERE status='aprobado'
        AND ((scope='folder' AND folder_id IS NOT DISTINCT FROM %s) OR (%s::uuid IS NULL AND scope='workspace'))""",
                      (folder_id, folder_id)).fetchall()
    scored = [(max(similarity(term, n) for n in [r["term"], *r["aliases"]]), r) for r in rows
              if len(r["term"].split()) == len(term.split())]
    best = max(scored, key=lambda pair: pair[0], default=(0, None))
    return best[1] if best[0] >= threshold else None


def _examples(folder_id, examples):
    """Examples are transcript excerpts: kept only on folder terms, which only the folder's members read. A workspace
    term is read by every employee and integration, so it never carries what someone said in a meeting."""
    return Jsonb([e[:300] for e in examples][:5] if folder_id else [])


def _seen_again(db, row, examples, occurrences):
    return db.execute(f"""UPDATE glossary_terms SET occurrences=occurrences+%s,
            examples=(SELECT COALESCE(jsonb_agg(e), '[]') FROM (SELECT DISTINCT e FROM jsonb_array_elements(examples || %s) e LIMIT 5) x),
            updated_at=now() WHERE id=%s RETURNING {FIELDS}""",
                      (occurrences, _examples(row["folder_id"], examples), row["id"])).fetchone()


def upsert_entity(db, term, kind, definition, folder_id=None, examples=(), occurrences=1):
    """A named entity the model grounded in a meeting goes straight into the glossary, editable.
    An existing spelling only gains occurrences (and the definition if it had none); a discarded one stays discarded."""
    term, definition = clean_term(term), " ".join(definition.split())[:500]
    existing = same_term(db, folder_id, term) or _sounds_like(db, folder_id, term)
    if existing and existing["status"] == "aprobado" and plain_key(existing["term"]) != plain_key(term) \
            and plain_key(term) not in {plain_key(a) for a in existing["aliases"]}:
        # «OmniRoot» heard where «OmniRoute» is already a term: the heard spelling becomes its «sounds like»,
        # instead of a second term that would compete with the real one in later corrections.
        return db.execute(f"UPDATE glossary_terms SET aliases=%s, occurrences=occurrences+%s, updated_at=now() WHERE id=%s RETURNING {FIELDS}",
                          (_clean_aliases(list(existing["aliases"]) + [term]), occurrences, existing["id"])).fetchone()
    if existing:
        row = _seen_again(db, existing, examples, occurrences)
        if row["status"] == "propuesto" or (row["status"] == "aprobado" and not row["definition"]):
            row = db.execute(f"""UPDATE glossary_terms SET status='aprobado', kind=%s, definition=%s, updated_at=now()
                WHERE id=%s RETURNING {FIELDS}""", (kind, definition or row["definition"], row["id"])).fetchone()
        return row
    return db.execute(f"""INSERT INTO glossary_terms(id,scope,folder_id,term,kind,definition,status,origin,occurrences,examples)
        VALUES (%s,%s,%s,%s,%s,%s,'aprobado','llm',%s,%s) RETURNING {FIELDS}""",
                      (uuid4(), "folder" if folder_id else "workspace", folder_id, term, kind, definition, occurrences,
                       _examples(folder_id, examples))).fetchone()


def upsert_candidate(db, created_by, term, origin, folder_id=None, kind="concepto", examples=(), occurrences=1, alias_of=None):
    """Propose a term (status propuesto) or add occurrences to the term it is a spelling of; never approves."""
    term = clean_term(term)
    existing = same_term(db, folder_id, term)
    if existing:
        return _seen_again(db, existing, examples, occurrences)
    scope = "folder" if folder_id else "workspace"
    return db.execute(f"""INSERT INTO glossary_terms(id,scope,folder_id,term,kind,status,origin,occurrences,examples,created_by,definition)
        VALUES (%s,%s,%s,%s,%s,'propuesto',%s,%s,%s,%s,%s)
        ON CONFLICT (scope, COALESCE(folder_id, '00000000-0000-0000-0000-000000000000'::uuid), lower(term))
        DO UPDATE SET occurrences=glossary_terms.occurrences+EXCLUDED.occurrences,
            examples=(SELECT COALESCE(jsonb_agg(e), '[]') FROM (SELECT DISTINCT e FROM jsonb_array_elements(glossary_terms.examples || EXCLUDED.examples) e LIMIT 5) x),
            updated_at=now()
        RETURNING {FIELDS}""", (uuid4(), scope, folder_id, term, kind, origin, occurrences,
                                 _examples(folder_id, examples), created_by,
                                 f"Posible alias de {alias_of}" if alias_of else "")).fetchone()


def with_alias_modes(rows: list[dict]) -> list[dict]:
    """Each term with what each of its aliases does: corrects, suggests or is inactive, and why, judged against
    the other terms listed with it (a name or a shared alias is inactive)."""
    from synth.worker.glossary_correction import alias_mode
    from synth.worker.phonetic import plain
    names = {plain(r["term"]) for r in rows if r["status"] == "aprobado"}
    owners: dict[str, int] = {}
    for r in rows:
        if r["status"] == "aprobado":
            for a in r["aliases"]:
                owners[" ".join(a.split()).casefold()] = owners.get(" ".join(a.split()).casefold(), 0) + 1
    out = []
    for r in rows:
        modes = []
        for a in r["aliases"]:
            mode, reason = alias_mode(a, r.get("alias_feedback"), names, owners.get(" ".join(a.split()).casefold(), 0) > 1)
            modes.append({"alias": a, "mode": mode, "reason": reason})
        out.append({**r, "alias_modes": modes})
    return out


def record_alias_answer(db, meeting_id, correction: dict, accepted: bool) -> None:
    """Count a person's answer on the term whose alias produced `correction`, in the scopes the meeting reads."""
    alias = " ".join(correction["original"].split()).casefold()
    vote = "si" if accepted else "no"
    db.execute("""UPDATE glossary_terms SET alias_feedback = jsonb_set(alias_feedback, ARRAY[%s::text],
            COALESCE(alias_feedback->%s::text, '{}'::jsonb)
            || jsonb_build_object(%s::text, COALESCE((alias_feedback->%s::text->>%s::text)::int, 0) + 1)),
            updated_at=now()
        WHERE status='aprobado' AND term=%s
          AND EXISTS (SELECT 1 FROM unnest(aliases) a WHERE lower(regexp_replace(a, '[[:space:]]+', ' ', 'g')) = %s)
          AND (scope='workspace' OR folder_id=(SELECT folder_id FROM meetings WHERE id=%s))""",
               (alias, alias, vote, alias, vote, correction["term"], alias, meeting_id))


def rejected_corrections(db, meeting_id) -> set[str]:
    return {r["correction_id"] for r in db.execute(
        "SELECT correction_id FROM correction_reviews WHERE meeting_id=%s AND decision='rechazada'", (meeting_id,)).fetchall()}


def register(app, session_dependency, store_version=None) -> None:
    @app.get("/v1/glossary")
    def list_terms(context: session_dependency, folder_id: UUID | None = None,  # slop-ok: route handler, registered by its decorator
                   status: Literal["propuesto", "aprobado", "descartado"] | None = None):
        db, actor = context
        return {"items": with_alias_modes(visible_terms(db, actor, folder_id, status))}

    @app.post("/v1/glossary", status_code=201)
    def create_term(body: TermIn, context: session_dependency):  # slop-ok: route handler, registered by its decorator
        """A person adding a term approves it directly (origin manual)."""
        db, actor = context
        _person(actor)
        if body.folder_id:
            authorize_folder(db, actor, body.folder_id, True)
        scope, term = ("folder" if body.folder_id else "workspace"), clean_term(body.term)
        if not term:
            raise HTTPException(422, "El término está vacío.")
        existing = same_term(db, body.folder_id, term)
        if existing:
            # Adding a spelling of an existing term approves that term and keeps what it already says.
            row = db.execute(f"""UPDATE glossary_terms SET status='aprobado', kind=%s, definition=%s, aliases=%s, updated_at=now()
                WHERE id=%s RETURNING {FIELDS}""", (body.kind or existing["kind"], body.definition.strip() or existing["definition"],
                                                    _clean_aliases(list(existing["aliases"]) + body.aliases), existing["id"])).fetchone()
        else:
            row = db.execute(f"""INSERT INTO glossary_terms(id,scope,folder_id,term,kind,aliases,definition,status,origin,created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'aprobado','manual',%s) RETURNING {FIELDS}""",
                             (uuid4(), scope, body.folder_id, term, body.kind or "concepto", body.aliases,
                              body.definition.strip(), actor["id"])).fetchone()
        audit(db, actor["id"], "glossary.create", row["id"], {"scope": scope})
        return row

    @app.post("/v1/glossary/candidates", status_code=201)
    def propose(body: Candidate, context: session_dependency):
        """Proposals never become approved here: a person approves them."""
        db, actor = context
        _person(actor)
        if body.folder_id:
            authorize_folder(db, actor, body.folder_id)
        if body.alias_of:
            target = _term(db, actor, body.alias_of)
            if body.term.casefold() in {a.casefold() for a in target["aliases"]} or body.term.casefold() == target["term"].casefold():
                return target
        row = upsert_candidate(db, actor["id"], body.term, body.origin, body.folder_id, body.kind, body.examples,
                               body.occurrences, body.alias_of)
        audit(db, actor["id"], "glossary.propose", row["id"], {"origin": body.origin})
        return row

    @app.post("/v1/glossary/{term_id}/approve")
    def approve(term_id: UUID, context: session_dependency):
        return _set_status(term_id, context, "aprobado")

    @app.post("/v1/glossary/{term_id}/discard")
    def discard(term_id: UUID, context: session_dependency):
        return _set_status(term_id, context, "descartado")

    def _set_status(term_id, context, status):
        db, actor = context
        _person(actor)
        _term(db, actor, term_id, True)
        row = db.execute(f"UPDATE glossary_terms SET status=%s, updated_at=now() WHERE id=%s RETURNING {FIELDS}",
                         (status, term_id)).fetchone()
        audit(db, actor["id"], "glossary." + status, term_id)
        return row

    @app.patch("/v1/glossary/{term_id}")
    def edit(term_id: UUID, body: TermPatch, context: session_dependency):
        db, actor = context
        _person(actor)
        current = _term(db, actor, term_id, True)
        values = body.model_dump(exclude_none=True)
        if "aliases" in values:
            values["aliases"] = _clean_aliases(values["aliases"])
        if "term" in values:
            values["term"] = clean_term(values["term"])
            if not values["term"]:
                raise HTTPException(422, "El término está vacío.")
            other = same_term(db, current["folder_id"], values["term"])
            if other and other["id"] != term_id and other["status"] != "descartado":
                # Renaming «Superbase» to an existing «Supabase» is the same term: merge them.
                return _merge(db, actor, current, other)
            # The heard spelling stays as «sounds like», so later transcripts are corrected to the new name.
            values["aliases"] = _clean_aliases(values.get("aliases", list(current["aliases"])) + [current["term"]])
            values["aliases"] = [a for a in values["aliases"] if a.casefold() != values["term"].casefold()]
        if not values:
            raise HTTPException(422, "No hay cambios.")
        columns = {"term": "term=%s", "aliases": "aliases=%s", "definition": "definition=%s", "kind": "kind=%s"}
        sets = ", ".join(columns[k] for k in values)
        row = db.execute(f"UPDATE glossary_terms SET {sets}, updated_at=now() WHERE id=%s RETURNING {FIELDS}",
                         (*values.values(), term_id)).fetchone()
        audit(db, actor["id"], "glossary.edit", term_id, {"fields": sorted(values)})
        return row

    @app.post("/v1/glossary/{term_id}/merge")
    def merge(term_id: UUID, body: Merge, context: session_dependency):
        """The term (a variant) becomes an alias of `into`, with its own aliases; the variant is discarded."""
        db, actor = context
        _person(actor)
        if body.into == term_id:
            raise HTTPException(422, "No se puede fusionar un término consigo mismo.")
        return _merge(db, actor, _term(db, actor, term_id, True), _term(db, actor, body.into, True))

    def _latest(db, actor, meeting_id):
        from .db import authorize_meeting
        _person(actor)
        authorize_meeting(db, actor, meeting_id, True)
        row = db.execute("SELECT version,content FROM transcript_versions WHERE meeting_id=%s ORDER BY version DESC LIMIT 1",
                         (meeting_id,)).fetchone()
        if not row:
            raise HTTPException(404, "La reunión todavía no tiene transcripción.")
        return row

    @app.post("/v1/meetings/{meeting_id}/corrections/{correction_id}/accept", status_code=201)
    def accept_correction(meeting_id: UUID, correction_id: str, context: session_dependency):  # slop-ok: route handler, registered by its decorator
        """Apply one glossary suggestion: a new transcript version, new minutes, and one acceptance for its alias."""
        from synth.contracts.transcript import Transcript
        from synth.worker.glossary_correction import accept
        db, actor = context
        row = _latest(db, actor, meeting_id)
        if correction_id in rejected_corrections(db, meeting_id):
            raise HTTPException(409, "Esa sugerencia ya se rechazó.")
        try:
            content = accept(row["content"], correction_id)
        except ValueError:
            raise HTTPException(404, "Esa sugerencia no está en la versión actual.") from None
        record_alias_answer(db, meeting_id, next(c for c in content["corrections"] if c["id"] == correction_id), True)
        return store_version(db, actor, meeting_id, Transcript.model_validate(content),
                             f"accept:{row['version']}:{correction_id}", "transcript.correction_accept")

    @app.post("/v1/meetings/{meeting_id}/corrections/{correction_id}/reject")
    def reject_correction(meeting_id: UUID, correction_id: str, context: session_dependency):  # slop-ok: route handler, registered by its decorator
        """Dismiss one glossary suggestion. The text does not change, so neither the version nor the minutes do."""
        db, actor = context
        row = _latest(db, actor, meeting_id)
        target = next((c for c in row["content"].get("corrections") or [] if c["id"] == correction_id and c["status"] == "suggested"), None)
        if target is None:
            raise HTTPException(404, "Esa sugerencia no está en la versión actual.")
        inserted = db.execute("""INSERT INTO correction_reviews(meeting_id,correction_id,decision,reviewed_by)
            VALUES (%s,%s,'rechazada',%s) ON CONFLICT DO NOTHING""", (meeting_id, correction_id, actor["id"])).rowcount
        if inserted:
            record_alias_answer(db, meeting_id, target, False)
            audit(db, actor["id"], "transcript.correction_reject", meeting_id, {"correction_id": correction_id})
        return {"correction_id": correction_id, "status": "rejected"}

    @app.post("/v1/meetings/{meeting_id}/corrections/{correction_id}/undo", status_code=201)
    def undo_correction(meeting_id: UUID, correction_id: str, context: session_dependency):  # slop-ok: route handler, registered by its decorator
        """Revert one glossary correction: a new transcript version, and new minutes for it."""
        from synth.contracts.transcript import Transcript
        from synth.worker.glossary_correction import undo
        from .db import authorize_meeting
        db, actor = context
        _person(actor)
        authorize_meeting(db, actor, meeting_id, True)
        row = db.execute("SELECT version,content FROM transcript_versions WHERE meeting_id=%s ORDER BY version DESC LIMIT 1",
                         (meeting_id,)).fetchone()
        if not row:
            raise HTTPException(404, "La reunión todavía no tiene transcripción.")
        try:
            content = undo(row["content"], correction_id)
        except ValueError:
            raise HTTPException(404, "Esa corrección no está aplicada en la versión actual.") from None
        undone = next(c for c in content["corrections"] if c["id"] == correction_id)
        if undone.get("origin") in ("alias", "contexto"):  # undoing an alias correction is a «no» for that alias
            record_alias_answer(db, meeting_id, undone, False)
        return store_version(db, actor, meeting_id, Transcript.model_validate(content),
                             f"undo:{row['version']}:{correction_id}", "transcript.correction_undo")
