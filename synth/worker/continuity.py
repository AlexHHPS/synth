"""Continuity between meetings and entity extraction.

Continuity: every open task and decision from earlier meetings of the folder gets a status in the new minutes:
«hecha», «en curso», «contradicha» (each with citations to this meeting) or «sin mencionar». A status without a
valid citation is not accepted; an item the model does not mention stays «sin mencionar».

Entities: the named things the meeting talks about (people, organisations, products, projects, systems and internal
concepts), each with what it is, go into the glossary. A name the transcript does not contain verbatim, a
common word or an entity without a definition is dropped.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .document_context import MeetingContext
from .phonetic import COMMON, plain

STATUSES = ("hecha", "en curso", "contradicha")
RANK = {"sin mencionar": 0, "en curso": 1, "hecha": 2, "contradicha": 3}
# Accepted spellings of the six glossary categories → the stored kind.
KINDS = {"persona": "persona", "organizacion": "organizacion", "organización": "organizacion", "cliente": "organizacion",
         "empresa": "organizacion", "producto": "producto", "proyecto": "proyecto", "sistema": "sistema",
         "herramienta": "sistema", "concepto": "concepto"}


PLAIN_KINDS = {plain(k): v for k, v in KINDS.items()}
EDGE_PUNCTUATION = " ¿?¡!.,;:«»\"'“”‘’()"


def prior_items(context: MeetingContext) -> list[dict[str, Any]]:
    return ([{"key": t["key"], "kind": "tarea", "text": t["title"]} for t in context.open_tasks] +
            [{"key": d["key"], "kind": "decisión", "text": d["decision"]} for d in context.open_decisions])


def continuity_prompt(context: MeetingContext, prior: list[dict[str, Any]], window: list[dict[str, Any]]) -> str:
    items = "\n".join(f"[{p['key']}] ({p['kind']}) {p['text']}" for p in prior)
    return (f"{context.header()}\n\nPendientes de reuniones anteriores:\n{items}\n\nPara cada pendiente que ESTE tramo menciona, "
            "di si quedó «hecha», sigue «en curso» o la reunión la «contradicha» (se decide lo contrario o se anula), con los "
            "ids de las líneas que lo dicen. No incluyas los que no se mencionan. "
            'Esquema: {"items": [{"key": "P1", "status": "hecha", "citations": ["t000012"]}]}\n\n' +
            "\n".join(line["line"] for line in window))


def validate_continuity(prior_keys: set[str], window_ids: set[str]) -> Callable[[Any], list[dict[str, Any]]]:
    def validate(data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("continuidad_invalida")
        kept = []
        for item in data["items"]:
            # Items with an unknown key, an unknown status or no citation from this window are dropped, never repaired.
            if not isinstance(item, dict) or item.get("key") not in prior_keys or item.get("status") not in STATUSES:
                continue
            citations = [c for c in item.get("citations") or [] if isinstance(c, str) and c in window_ids]
            if citations:
                kept.append({"key": item["key"], "status": item["status"], "citations": list(dict.fromkeys(citations))})
        return kept
    return validate


def merge_continuity(prior: list[dict[str, Any]], found: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per prior item; the strongest status wins across windows. A new task that closes or continues a
    previous one (its `previous` key and lifecycle) counts as evidence too."""
    best: dict[str, dict[str, Any]] = {p["key"]: {**p, "status": "sin mencionar", "citations": []} for p in prior}
    evidence = list(found)
    for task in tasks:
        if task.get("previous") in best and task.get("lifecycle") in ("completada", "continuacion") and task.get("citations"):
            evidence.append({"key": task["previous"], "status": "hecha" if task["lifecycle"] == "completada" else "en curso",
                             "citations": task["citations"]})
    for item in evidence:
        row = best[item["key"]]
        if RANK[item["status"]] > RANK[row["status"]]:
            row.update(status=item["status"], citations=list(item["citations"]))
        elif item["status"] == row["status"]:
            row["citations"] = list(dict.fromkeys(row["citations"] + item["citations"]))
    return list(best.values())


def entities_prompt(context: MeetingContext, window: list[dict[str, Any]]) -> str:
    return (f"{context.header()}\n\nLista los nombres propios relevantes que se nombran en este tramo, escritos exactamente "
            "como aparecen, y di qué es cada uno según lo que se dice. Categorías: persona (actor relevante), organizacion "
            "(cliente, proveedor, socio), producto, proyecto (o iniciativa), sistema (o herramienta) y concepto (sigla, "
            "proceso o término interno). Nada genérico («el cliente», «el equipo») ni muletillas («Vale», «Bueno»). "
            "La definición es una frase corta basada en la reunión, p. ej. «cliente industrial del piloto de nóminas». "
            'Esquema: {"entities": [{"name": "Valdivar", "kind": "organizacion", "definition": "cliente industrial del piloto"}]}\n\n' +
            "\n".join(line["line"] for line in window))


def validate_entities(window: list[dict[str, Any]]) -> Callable[[Any], list[dict[str, Any]]]:
    text = " ".join(line["text"] for line in window)

    def validate(data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, dict) or not isinstance(data.get("entities"), list):
            raise ValueError("entidades_invalidas")
        kept = []
        for item in data["entities"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or plain(str(item.get("kind"))) not in PLAIN_KINDS:
                continue
            definition = " ".join(str(item.get("definition") or "").split())
            if not 3 <= len(definition) <= 200:
                continue
            name = item["name"].strip(EDGE_PUNCTUATION)
            key = plain(name)
            # Grounded: the name is in the transcript as a whole word, and it is not a common Spanish word.
            # A proper noun: capitalized as it appears in the meeting, e.g. «Nimbus», not «el piloto».
            proper = name[:1].isupper() and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text)
            if not 3 <= len(name) <= 80 or key in COMMON or not proper:
                continue
            example = next((line["text"] for line in window if key in plain(line["text"])), "")
            kept.append({"name": name, "kind": PLAIN_KINDS[plain(str(item["kind"]))], "definition": definition, "example": example[:300]})
        return kept
    return validate


def merge_entities(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for entity in found:
        merged.setdefault(plain(entity["name"]), entity)
    return list(merged.values())
