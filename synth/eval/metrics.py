"""Deterministic quality metrics. Pure functions: no I/O, no model calls."""
from __future__ import annotations

import re
import statistics
import unicodedata
from typing import Any, Iterable

from synth.worker.specificity import is_generic, normalize

STOPWORDS = frozenset("""a al algo como con de del el ella ellos en entre era es esa ese eso esta este esto
fue ha hay la las le les lo los mas me mi muy no nos o para pero por que se si sin sobre su sus te tu un una
uno unos unas y ya yo vale bueno pues entonces tambien solo ser estar son ser va vamos""".split())
UNASSIGNED = {"", "sin asignar", "sin responsable", "nadie"}


def words(text: str) -> list[str]:
    return re.findall(r"[\w-]+", normalize(text))


def content_words(text: str) -> list[str]:
    return [w for w in words(text) if len(w) >= 3 and w not in STOPWORDS]


def turn_stats(turns: list[dict[str, Any]], duration_ms: int) -> dict[str, float]:
    if not turns or duration_ms <= 0:
        return {"turns_per_min": 0.0, "median_words_per_turn": 0}
    counts = [len(words(t["text"])) for t in turns]
    return {"turns_per_min": round(len(turns) / (duration_ms / 60_000), 3),
            "median_words_per_turn": statistics.median(counts)}


def _ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def _duplicated_words(tokens: list[str], reference: list[str]) -> int:
    if not tokens or not reference:
        return 0
    n = min(3, len(tokens))
    grams = _ngrams(reference, n)
    marked = [False] * len(tokens)
    for i in range(len(tokens) - n + 1):
        if tuple(tokens[i:i + n]) in grams:
            for j in range(i, i + n):
                marked[j] = True
    return sum(marked)


def cross_channel_duplicate_pct(segments: list[dict[str, Any]], window_ms: int = 2000) -> float:
    """% of all words that are microphone repetitions of nearby system speech (speaker bleed)."""
    system = [s for s in segments if s.get("source_id") == "system"]
    total = sum(len(words(s["text"])) for s in segments)
    if not total or not system:
        return 0.0
    duplicated = 0
    for segment in segments:
        if segment.get("source_id") != "microphone":
            continue
        near = [s for s in system if s["start_ms"] <= segment["end_ms"] + window_ms
                and s["end_ms"] >= segment["start_ms"] - window_ms]
        reference = [w for s in near for w in words(s["text"])]
        duplicated += _duplicated_words(words(segment["text"]), reference)
    return round(100 * duplicated / total, 3)


def speaker_count(segments: Iterable[dict[str, Any]]) -> int:
    return len({s["speaker_id"] for s in segments if s.get("speaker_id")})


def speaker_count_ok(predicted: int, reference: int, tolerance: int = 1) -> bool:
    return abs(predicted - reference) <= tolerance


def identity_scores(suggestions: list[dict[str, Any]], reference: dict[str, dict[str, Any]],
                    min_seconds: float = 10) -> dict[str, float]:
    """Operator suggestion coverage/precision (coverage ≥ 0.9, wrong_high == 0)."""
    eligible = {m for m, ref in reference.items() if ref.get("operator_seconds", 0) >= min_seconds}
    correct = [s for s in suggestions if s["cluster"] in reference.get(s["meeting"], {}).get("operator_clusters", [])]
    covered = {s["meeting"] for s in correct if s["meeting"] in eligible}
    wrong_high = sum(1 for s in suggestions if s.get("level") == "high" and s not in correct)
    return {"eligible_meetings": len(eligible),
            "coverage": round(len(covered) / len(eligible), 4) if eligible else 0.0,
            "precision": round(len(correct) / len(suggestions), 4) if suggestions else 0.0,
            "wrong_high": wrong_high}


def _count(term: str, text: str) -> int:
    return len(re.findall(rf"(?<![\w-]){re.escape(normalize(term))}(?![\w-])", normalize(text)))


def glossary_recall(corrected: str, reference: str, terms: list[str]) -> float:
    expected = sum(_count(t, reference) for t in terms)
    if not expected:
        return 1.0
    found = sum(min(_count(t, corrected), _count(t, reference)) for t in terms)
    return round(found / expected, 4)


def false_substitution_rate(corrections: list[dict[str, Any]], reference: dict[str, str]) -> float:
    evaluated = [c for c in corrections if c.get("segment_id") in reference]
    if not evaluated:
        return 0.0
    wrong = sum(1 for c in evaluated if not _count(c["to"], reference[c["segment_id"]]))
    return round(wrong / len(evaluated), 4)


def generic_point_pct(points: list[str], concrete_terms: Iterable[str] = ()) -> float:
    if not points:
        return 0.0
    terms = list(concrete_terms)
    return round(100 * sum(is_generic(p, terms) for p in points) / len(points), 4)


def _matches(group: str, text: str) -> bool:
    return any(_count(alternative.strip(), text) for alternative in group.split("|"))


def key_fact_recall(document_text: str, facts: list[dict[str, Any]]) -> float:
    if not facts:
        return 1.0
    covered = sum(1 for f in facts if all(_matches(g, document_text) for g in f["keywords"]))
    return round(covered / len(facts), 4)


def _stem(word: str) -> str:
    return word[:5]


def citation_fidelity(items: list[dict[str, Any]], segments: dict[str, str], minimum: float = 0.3) -> float:
    """Share of items whose citations exist and lexically support the item text."""
    if not items:
        return 1.0
    faithful = 0
    for item in items:
        cited = item.get("citations") or []
        if not cited or any(c not in segments for c in cited):
            continue
        support = {_stem(w) for c in cited for w in content_words(segments[c])}
        claim = [_stem(w) for w in content_words(item["text"])]
        if claim and sum(w in support for w in claim) / len(claim) >= minimum:
            faithful += 1
    return round(faithful / len(items), 4)


def tasks_with_owner_pct(tasks: list[dict[str, Any]]) -> float:
    if not tasks:
        return 100.0
    def ok(task):
        owner = normalize(task.get("owner") or "").strip()
        if owner not in UNASSIGNED:
            return True
        return owner != "" and bool((task.get("owner_evidence") or "").strip())
    return round(100 * sum(map(ok, tasks)) / len(tasks), 4)


def decisions_with_reason_pct(decisions: list[dict[str, Any]]) -> float:
    if not decisions:
        return 100.0
    return round(100 * sum(1 for d in decisions if len(content_words(d.get("reason") or "")) >= 2) / len(decisions), 4)


def _citations(item: dict[str, Any]) -> list[str]:
    raw = item.get("evidence") or item.get("citations") or []
    return [c["segment_id"] if isinstance(c, dict) else c for c in raw]


def document_parts(document: dict[str, Any]) -> dict[str, Any]:
    """Points, tasks and decisions from a v1 (flat sections) or v2 (topics) document."""
    content = document.get("content", document)
    points = []
    for item in content.get("summary", []):
        points.append({"text": item["text"], "citations": _citations(item)})
    for topic in content.get("topics", []):
        for item in topic.get("points", []):
            points.append({"text": item["text"], "citations": _citations(item)})
    tasks = [{**t, "citations": _citations(t)} for t in content.get("tasks", content.get("actions", []))]
    decisions = [{**d, "citations": _citations(d)} for d in content.get("decisions", [])]
    pieces = [content.get("tldr", "")] + [p["text"] for p in points]
    pieces += [t.get("text") or t.get("title", "") for t in tasks] + [d.get("text") or d.get("decision", "") for d in decisions]
    pieces += [t.get("description", "") for t in tasks] + [d.get("reason", "") or "" for d in decisions]
    return {"points": points, "tasks": tasks, "decisions": decisions,
            "text": "\n".join(p for p in pieces if p)}
