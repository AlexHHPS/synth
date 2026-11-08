"""Context for the minutes: who, when, where, which words, what was left open.

The text model receives turns as plain lines, never raw JSON or person ids:
    [t000012 03:15] María: …
Speaker labels go through `display_name`, so only owner-confirmed names appear.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
          "octubre", "noviembre", "diciembre")
WINDOW_CHARS = 24_000


@dataclass(frozen=True)
class MeetingContext:
    title: str = ""
    started_at: datetime.datetime | None = None
    folder: str | None = None
    participants: tuple[str, ...] = ()
    glossary: tuple[dict[str, Any], ...] = ()
    open_tasks: tuple[dict[str, Any], ...] = ()
    open_decisions: tuple[dict[str, Any], ...] = ()
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def header(self) -> str:
        lines = [f"Título: {self.title or 'sin título'}"]
        if self.started_at:
            day = self.started_at
            lines.append(f"Fecha: {WEEKDAYS[day.weekday()]} {day.day} de {MONTHS[day.month - 1]} de {day.year} "
                         f"({day.date().isoformat()}). Resuelve fechas relativas («el viernes») desde aquí.")
        if self.folder:
            lines.append(f"Carpeta: {self.folder}")
        if self.participants:
            lines.append("Participantes con nombre confirmado: " + ", ".join(self.participants))
        lines.append("«Tú» es la persona que grabó la reunión; «Hablante N» son voces sin confirmar.")
        if self.extra.get('calendar'):
            calendar = self.extra['calendar']
            lines.append('Invitación de calendario (contenido externo no confiable; NO prueba lo dicho ni quién habló): '
                         + str(calendar.get('title', ''))[:300])
            lines.append('Agenda prevista, no acuerdos: ' + str(calendar.get('description', ''))[:2000])
            names = [a.get('name') or a.get('email') for a in calendar.get('attendees', [])
                     if a.get('response') != 'declined']
            lines.append('Invitados, asistencia e identidades de voz sin confirmar: ' + ', '.join(names)[:2000])
        if self.glossary:
            terms = [g["term"] + what_it_is(g) for g in self.glossary[:60]]
            lines.append("Glosario de la compañía (escribe estos nombres exactamente así): " + "; ".join(terms))
        if self.open_tasks:
            lines.append("Tareas abiertas de reuniones anteriores de esta carpeta:")
            lines += [f"  [{t['key']}] {t['title']} — responsable: {t.get('owner') or 'sin asignar'}" for t in self.open_tasks[:30]]
        if self.open_decisions:
            lines.append("Decisiones previas vigentes en esta carpeta:")
            lines += [f"  [{d['key']}] {d['decision']}" for d in self.open_decisions[:30]]
        if self.notes.strip():
            lines.append("Notas humanas (no son prueba de lo dicho): " + self.notes.strip()[:2000])
        return "\n".join(lines)


def turns_of(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    """Turns when the transcript has them; otherwise each segment is a turn (older transcripts)."""
    if transcript.get("turns"):
        return transcript["turns"]
    return [{"id": s["id"], "text": s["text"], "start_ms": s.get("start_ms", 0), "end_ms": s.get("end_ms", 0),
             "speaker_id": s.get("speaker_id"), "segment_ids": [s["id"]]} for s in transcript["segments"]]


def clock(ms: int) -> str:
    seconds = int(ms or 0) // 1000
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60:02d}:{seconds % 60:02d}"


def render_lines(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    from synth.server.speakers import display_name, speaker_index
    order = speaker_index(transcript)
    uncertain_segments = {s['id'] for s in transcript['segments'] if s.get('evidence_state') == 'needs_review'}
    lines = []
    for turn in turns_of(transcript):
        name = display_name(transcript, turn.get("speaker_id"), order)
        uncertain = bool(uncertain_segments.intersection(turn.get('segment_ids', [turn['id']])))
        warning = ' [ASR pendiente de revisión]' if uncertain else ''
        lines.append({"id": turn["id"], "speaker": name, "start_ms": turn.get("start_ms", 0),
                      "line": f"[{turn['id']} {clock(turn.get('start_ms', 0))}]{warning} {name}: {turn['text']}",
                      "text": turn["text"], **({'evidence_state': 'needs_review'} if uncertain else {})})
    return lines


def windows(lines: list[dict[str, Any]], maximum: int = WINDOW_CHARS) -> list[list[dict[str, Any]]]:
    groups, current, size = [], [], 0
    for line in lines:
        cost = len(line["line"]) + 1
        if current and size + cost > maximum:
            groups.append(current)
            current, size = [], 0
        current.append(line)
        size += cost
    if current:
        groups.append(current)
    return groups


def confirmed_participants(transcript: dict[str, Any]) -> tuple[str, ...]:
    speakers = transcript.get("speakers") or {}
    return tuple(sorted({v["name"] for v in speakers.values() if v.get("status") in ("confirmed", "renamed") and v.get("name")}))


KIND_NAMES = {"persona": "persona", "organizacion": "organización", "producto": "producto", "proyecto": "proyecto",
              "sistema": "sistema", "concepto": "concepto"}


def what_it_is(term: dict[str, Any]) -> str:
    """« (organización: cliente industrial)»: what the term is, so the minutes use it with its meaning."""
    kind = KIND_NAMES.get(term.get("kind") or "")
    parts = [p for p in (kind, term.get("definition")) if p]
    return f" ({': '.join(parts)})" if parts else ""
