"""Minutes in one pass per transcript window (0.6.0).

The sectioned pipeline asked 12-36 small questions in a row and failed the whole
acta when any of them did. Here the model reads a window once and returns its
topics, tasks, decisions, questions, follow-up of earlier commitments and named
entities together. A normal meeting fits one window, so it is one call.

Nothing optional can fail the acta: an invalid item is dropped and counted, a
missing follow-up or entity list is empty, and a summary the model did not give
is built from the topic outcomes. Grounding rules are the sectioned ones.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from . import continuity
from .document_context import MeetingContext, render_lines, windows
from .minutes import (DECISION_STATUS, OWNER_EVIDENCE, RULES, SCHEMA_VERSION, TASK_LIFECYCLE, TEMPLATES, TYPES,
                      _validate_topic, assemble, parse_json, render_markdown)
from .specificity import is_generic

STRATEGY = "consolidated"
WINDOW_CHARS = int(os.environ.get("SYNTH_MINUTES_WINDOW_CHARS", "60000"))
# Hermes refuses a larger answer (synth/hermes/app.py); a window that needs more is split in two.
MAX_TOKENS = 8192
SMALLEST_SPLIT = 12
LISTS = ("points", "tasks", "decisions", "questions")
NAME_FIELD = {"points": "text", "questions": "text", "tasks": "title", "decisions": "decision"}


class TopicStream:
    """Finds each finished object of the "topics" array while the answer is still arriving."""

    def __init__(self) -> None:
        self.buffer = ""
        self.position = 0
        self.armed = self.closed = self.in_string = self.escaped = False
        self.depth = 0
        self.start = 0

    def feed(self, fragment: str) -> list[dict[str, Any]]:
        self.buffer += fragment
        if self.closed:
            return []
        if not self.armed:
            key = self.buffer.find('"topics"')
            opening = self.buffer.find("[", key) if key >= 0 else -1
            if opening < 0:
                return []
            self.armed, self.position = True, opening + 1
        finished = []
        while self.position < len(self.buffer):
            char = self.buffer[self.position]
            self.position += 1
            if self.in_string:
                if self.escaped:
                    self.escaped = False
                elif char == "\\":
                    self.escaped = True
                elif char == '"':
                    self.in_string = False
            elif char == '"':
                self.in_string = True
            elif char == "{":
                if self.depth == 0:
                    self.start = self.position - 1
                self.depth += 1
            elif char == "}":
                self.depth -= 1
                if self.depth == 0:
                    try:
                        topic = json.loads(self.buffer[self.start:self.position])
                    except ValueError:
                        continue
                    if isinstance(topic, dict):
                        finished.append(topic)
            elif char == "]" and self.depth == 0:
                self.closed = True
                break
        return finished


def draft_topic(raw: dict[str, Any], known: set[str]) -> dict[str, Any] | None:
    """What a reader may see before validation: text only, citations limited to real lines."""
    def text(value: Any, limit: int) -> str:
        return value.strip()[:limit] if isinstance(value, str) else ""
    title = text(raw.get("title"), 200)
    if not title:
        return None
    points = []
    for point in raw.get("points") if isinstance(raw.get("points"), list) else []:
        if isinstance(point, dict) and text(point.get("text"), 600):
            cited = [c for c in point.get("citations") or [] if isinstance(c, str) and c in known]
            points.append({"text": text(point.get("text"), 600), "citations": cited})
    return {"title": title, "context": text(raw.get("context"), 1200), "discussion": text(raw.get("discussion"), 2500),
            "outcome": text(raw.get("outcome"), 1200), "points": points, "tasks": [], "decisions": [], "questions": [],
            "draft": True}


def window_prompt(context: MeetingContext, window: list[dict[str, Any]], *, first: bool, last: bool,
                  covered: list[str]) -> str:
    previous = ", ".join(t["key"] for t in context.open_tasks) or "ninguna"
    schema: dict[str, Any] = {}
    if first:
        schema["meeting_type"] = "|".join(TYPES)
    schema["topics"] = [{
        "title": "tema concreto (qué, no «Discusión general»)", "context": "1-2 frases", "discussion": "2-5 frases",
        "outcome": "1-3 frases o vacío si no hubo cierre",
        "points": [{"text": "hecho con un dato concreto (cifra, nombre, fecha, sistema o motivo)", "citations": ["id"]}],
        "tasks": [{"title": "verbo + objeto", "description": "por qué", "owner": "nombre | Tú | sin asignar",
                   "owner_evidence": "|".join(OWNER_EVIDENCE), "due_text": "expresión original o null",
                   "due_date": "AAAA-MM-DD o null", "done_criteria": "o null", "depends_on": [],
                   "lifecycle": "|".join(TASK_LIFECYCLE), "previous": f"clave previa ({previous}) o null",
                   "citations": ["id"], "confidence": 0.0}],
        "decisions": [{"decision": "...", "reason": "motivo dicho o null", "alternatives": [],
                       "status": "|".join(DECISION_STATUS), "affects": "o null", "decided_by": "o null",
                       "citations": ["id"]}],
        "questions": [{"text": "duda abierta", "citations": ["id"]}]}]
    if context.open_tasks or context.open_decisions:
        schema["continuity"] = [{"key": "P1 o D1", "status": "|".join(continuity.STATUSES), "citations": ["id"]}]
    schema["entities"] = [{"name": "como aparece", "kind": "persona|organizacion|producto|proyecto|sistema|concepto",
                           "definition": "qué es según la reunión"}]
    if first and last:
        schema["tldr"] = "2-3 frases concretas con lo más importante"
    carried = (f"Temas ya recogidos de tramos anteriores: {'; '.join(covered[-12:])}. Si el primero de este tramo "
               "continúa el último, usa exactamente el mismo título.\n") if covered else ""
    follow = ("continuity: solo los pendientes de reuniones anteriores que este tramo menciona, con su estado y las "
              "líneas que lo dicen.\n") if "continuity" in schema else ""
    return (f"{context.header()}\n\nTranscripción{'' if first and last else ' (tramo)'}:\n"
            + "\n".join(line["line"] for line in window)
            + context.media_prompt(window, first=first, last=last)
            + "\n\nEscribe el acta de este texto en un solo objeto JSON.\n"
            + (f"Elige el tipo de reunión; orienta el acta según él: "
               f"{' '.join(f'{k}: {v}' for k, v in TEMPLATES.items())}\n" if first else "")
            + carried
            + "topics: de 1 a 8 temas consecutivos, en orden, que cubran todo el texto. Agrupa repeticiones; cada punto "
              "lleva un dato concreto, nada de «se habló de». No repitas el mismo hecho en varios campos.\n"
              "tasks: solo una acción futura concreta encargada o asumida. Explicar una pantalla, describir trabajo "
              "terminado o desear una mejora no crea una tarea. Responsable: el hablante si se compromete (su nombre o "
              "«Tú», autocompromiso), una persona nombrada en la cita (explicito) o «sin asignar». Nunca pongas nombre "
              "a un «Hablante N». due_text copia la expresión dicha; due_date solo si esa expresión fija el día.\n"
              "decisions: una propuesta o recomendación sin aceptación no es un acuerdo.\n"
            + follow
            + "entities: nombres propios relevantes, escritos como aparecen, con una definición corta basada en la "
              "reunión. Nada genérico («el cliente») ni muletillas.\n"
              f"Esquema exacto: {json.dumps(schema, ensure_ascii=False)}")


def read_window(data: Any, window: list[dict[str, Any]], context: MeetingContext,
                terms: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Keep every valid part of an answer. Returns the window result and why items were dropped."""
    if not isinstance(data, dict) or not isinstance(data.get("topics"), list):
        raise ValueError("esquema_temas")
    previous = {t["key"] for t in context.open_tasks}
    participants = set(context.participants) | {"Tú"}
    topics, problems = [], []
    for raw in data["topics"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("title"), str) or not raw["title"].strip():
            problems.append("tema_sin_titulo")
            continue
        check = _validate_topic({"title": raw["title"].strip()[:200], "lines": window}, participants, previous,
                                context.started_at)
        try:
            topic = check({k: v for k, v in raw.items() if k not in LISTS})
        except (ValueError, TypeError, AttributeError):
            topic = check({})
        for key in LISTS:
            for item in raw.get(key) if isinstance(raw.get(key), list) else []:
                try:
                    topic[key] += check({key: [item]})[key]
                except (ValueError, TypeError, AttributeError, KeyError) as error:
                    problems.append(f"{key}:{error if isinstance(error, ValueError) else 'esquema_invalido'}")
        topic["points"] = [p for p in topic["points"] if not is_generic(p["text"], terms)]
        topics.append(topic)
    if not topics:
        raise ValueError("esquema_temas")
    prior = continuity.prior_items(context)
    try:
        followed = continuity.validate_continuity({p["key"] for p in prior}, {line["id"] for line in window})(
            {"items": data.get("continuity") or []})
    except (ValueError, TypeError, AttributeError):
        followed = []
    try:
        entities = continuity.validate_entities(window)({"entities": data.get("entities") or []})
    except (ValueError, TypeError, AttributeError):
        entities = []
    kind = data.get("meeting_type") if data.get("meeting_type") in TYPES else None
    tldr = data["tldr"].strip()[:900] if isinstance(data.get("tldr"), str) and data["tldr"].strip() else None
    return {"meeting_type": kind, "topics": topics, "continuity": followed, "entities": entities, "tldr": tldr}, problems


def ask_window(complete: Callable[..., dict[str, Any]], context: MeetingContext, window: list[dict[str, Any]],
               terms: list[str], responses: list, *, first: bool, last: bool, covered: list[str],
               on_draft: Callable[[int, dict[str, Any]], None] | None) -> dict[str, Any]:
    """One call, one correction when too much was rejected, then the better of the two answers."""
    messages = [{"role": "system", "content": RULES},
                {"role": "user", "content": window_prompt(context, window, first=first, last=last, covered=covered)}]
    known = {line["id"] for line in window}
    best: tuple[dict[str, Any], list[str]] | None = None
    failure: ValueError | None = None
    for attempt in range(2):
        stream, count = TopicStream(), 0

        def on_delta(fragment: str) -> None:
            nonlocal count
            for raw in stream.feed(fragment):
                draft = draft_topic(raw, known)
                if draft and on_draft:
                    # Readers place drafts by position, so positions never skip.
                    on_draft(count, draft)
                    count += 1
        response = complete(messages, MAX_TOKENS, on_delta if on_draft and attempt == 0 else None)
        responses.append(response)
        try:
            result, problems = read_window(parse_json(response["content"]), window, context, terms)
        except ValueError as error:
            failure, complaint = error, str(error)
        else:
            if best is None or len(problems) < len(best[1]):
                best = (result, problems)
            kept = sum(len(topic[key]) for topic in result["topics"] for key in LISTS)
            if len(problems) <= max(2, kept // 4):
                break
            complaint = ", ".join(sorted(set(problems))[:8])
        messages = messages + [{"role": "assistant", "content": response["content"]},
                               {"role": "user", "content": f"El validador rechazó partes de la respuesta: {complaint}. "
                                "Devuelve el objeto JSON completo corregido, con el esquema pedido y solo ids del texto."}]
    if best is None:
        raise failure or ValueError("minutes_json_invalid")
    return {**best[0], "dropped_invalid": len(best[1])}


def window_minutes(complete, context, window, terms, responses, *, first, last, covered, on_draft) -> dict[str, Any]:
    """A window whose answer does not fit the output is read in two halves instead of failing."""
    try:
        return ask_window(complete, context, window, terms, responses, first=first, last=last, covered=covered,
                          on_draft=on_draft)
    except ValueError as error:
        if str(error) not in ("gateway_output_incomplete", "gateway_request_rejected", "minutes_json_invalid", "esquema_temas") \
                or len(window) < SMALLEST_SPLIT:
            raise
    middle = len(window) // 2
    head = window_minutes(complete, context, window[:middle], terms, responses, first=first, last=False,
                          covered=covered, on_draft=None)
    tail = window_minutes(complete, context, window[middle:], terms, responses, first=False, last=last,
                          covered=covered + [t["title"] for t in head["topics"]], on_draft=None)
    return {"meeting_type": head["meeting_type"], "topics": head["topics"] + tail["topics"],
            "continuity": head["continuity"] + tail["continuity"], "entities": head["entities"] + tail["entities"],
            "tldr": None, "dropped_invalid": head["dropped_invalid"] + tail["dropped_invalid"]}


def join_topics(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Topics in order; one that continues across a cut is a single topic, and nothing is listed twice."""
    topics: list[dict[str, Any]] = []
    seen: dict[str, set[str]] = {key: set() for key in LISTS}
    for part in parts:
        for topic in part["topics"]:
            fresh = {key: [] for key in LISTS}
            for key in LISTS:
                for item in topic[key]:
                    name = " ".join(item[NAME_FIELD[key]].casefold().split())
                    if name not in seen[key]:
                        seen[key].add(name)
                        fresh[key].append(item)
            if topics and topics[-1]["title"].casefold() == topic["title"].casefold():
                merged = topics[-1]
                for field in ("context", "discussion", "outcome"):
                    if topic[field] and topic[field] not in merged[field]:
                        merged[field] = (merged[field] + " " + topic[field]).strip() if field != "outcome" else topic[field]
                for key in LISTS:
                    merged[key] = merged[key] + fresh[key]
            else:
                topics.append({**topic, **fresh})
    return topics


def fallback_tldr(topics: list[dict[str, Any]]) -> str:
    said = [t["outcome"] or t["discussion"] or t["context"] for t in topics]
    text = " ".join(s for s in said if s)[:900].strip()
    return text or "Temas tratados: " + "; ".join(t["title"] for t in topics)[:860]


def summary(complete, context: MeetingContext, topics: list[dict[str, Any]], responses: list) -> str:
    outcomes = "\n".join(f"- {t['title']}: {t['outcome'] or t['discussion']}" for t in topics)
    try:
        response = complete([{"role": "system", "content": RULES},
                             {"role": "user", "content": f"{context.header()}\n\nResume la reunión en 2-3 frases concretas a "
                              f'partir de estos resultados por tema, sin añadir hechos. Esquema: {{"tldr": "..."}}\n\n{outcomes}'}],
                            1024, None)
        responses.append(response)
        text = parse_json(response["content"]).get("tldr")
        if isinstance(text, str) and text.strip():
            return text.strip()[:900]
    except (ValueError, AttributeError):
        pass
    return fallback_tldr(topics)


def generate_consolidated(transcript: dict[str, Any], context: MeetingContext, complete: Callable[..., dict[str, Any]],
                          checkpoint: dict | None = None, save: Callable[[dict], None] | None = None,
                          on_draft: Callable[[int, dict[str, Any]], None] | None = None) -> tuple[dict, str, list]:
    responses: list = []
    lines = render_lines(transcript)
    prior = continuity.prior_items(context)
    if not lines:
        content = {"schema_version": SCHEMA_VERSION, "language": "es", "meeting_type": None, "tldr": "Sin contenido transcrito.",
                   "topics": [], "tasks": [], "decisions": [], "open_questions": [],
                   "continuity": continuity.merge_continuity(prior, [], []), "entities": []}
        return content, render_markdown(content), responses
    state = dict(checkpoint) if (checkpoint or {}).get("strategy") == STRATEGY else {}
    state.setdefault("strategy", STRATEGY)
    parts = state.setdefault("windows", [])
    terms = [g["term"] for g in context.glossary] + list(context.participants)
    groups = windows(lines, WINDOW_CHARS)
    for index, window in enumerate(groups):
        if index < len(parts):
            continue
        covered = [t["title"] for part in parts for t in part["topics"]]
        offset = len(join_topics(parts))
        parts.append(window_minutes(
            complete, context, window, terms, responses, first=index == 0, last=index == len(groups) - 1,
            covered=covered, on_draft=(lambda i, topic: on_draft(offset + i, topic)) if on_draft else None))
        # The flat list is what readers follow while the rest is still being written.
        state["topics"] = join_topics(parts)
        save and save(state)
    topics = state.get("topics") or join_topics(parts)
    kind = next((p["meeting_type"] for p in parts if p["meeting_type"]), None) or "seguimiento"
    tldr = (parts[0]["tldr"] if len(parts) == 1 else None) or summary(complete, context, topics, responses)
    tasks = [task for topic in topics for task in topic["tasks"]]
    content = {**assemble(kind, tldr, topics),
               "continuity": continuity.merge_continuity(prior, [i for p in parts for i in p["continuity"]], tasks),
               "entities": continuity.merge_entities([e for p in parts for e in p["entities"]])}
    return content, render_markdown(content), responses
