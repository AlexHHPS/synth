"""Turn builder: word timings + per-channel diarization → readable speaker turns.

Words keep their ASR text; nothing is invented or dropped. A turn joins
consecutive words of one speaker while the pause is short, splits on long
pauses or very long monologues, and lets short interjections («vale», «sí»)
of another speaker live beside a turn instead of cutting it in two.
"""
from __future__ import annotations

import bisect
import re
from typing import Any

MERGE_GAP_MS = 1500
SPLIT_GAP_MS = 2000
MAX_TURN_MS = 90_000
INTERJECTION_MAX_WORDS = 2
INTERJECTION_MAX_MS = 1500
LOCAL_SPEAKER = "local"
SENTENCE_END = re.compile(r"[.!?…]$")


def words_from_whisper(native: dict[str, Any], duration_ms: int, source_id: str) -> list[dict[str, Any]]:
    """Whole words from whisper-cli `-ojf` tokens (sub-word tokens are glued on a leading space)."""
    words: list[dict[str, Any]] = []
    for segment_index, part in enumerate(native.get("transcription", [])):
        for token in part.get("tokens", []):
            text = token.get("text", "")
            if not text or text.startswith("[_") or text.startswith("<|"):
                continue
            start = min(max(int(token["offsets"]["from"]), 0), duration_ms)
            end = min(max(int(token["offsets"]["to"]), start), duration_ms)
            if words and not text.startswith(" ") and words[-1]["segment_index"] == segment_index:
                words[-1]["text"] += text
                words[-1]["end_ms"] = max(words[-1]["end_ms"], end)
            elif text.strip():
                words.append({"text": text.strip(), "start_ms": start, "end_ms": end,
                              "source_id": source_id, "segment_index": segment_index})
    return [w for w in words if w["text"]]


def speaker_at(word: dict[str, Any], turns: list[dict[str, Any]], tolerance_ms: int = 500) -> str | None:
    best, best_overlap, nearest, nearest_gap = None, 0, None, tolerance_ms + 1
    for turn in turns:
        overlap = min(word["end_ms"], turn["end_ms"]) - max(word["start_ms"], turn["start_ms"])
        if overlap > best_overlap:
            best, best_overlap = turn["speaker_id"], overlap
        gap = max(turn["start_ms"] - word["end_ms"], word["start_ms"] - turn["end_ms"], 0)
        if gap < nearest_gap:
            nearest, nearest_gap = turn["speaker_id"], gap
    return best if best is not None else nearest


def dominant_speaker(diarization: dict[str, Any], share: float = 0.8) -> str | None:
    totals: dict[str, int] = {}
    for turn in diarization.get("segments", []):
        totals[turn["speaker_id"]] = totals.get(turn["speaker_id"], 0) + turn["end_ms"] - turn["start_ms"]
    speech = sum(totals.values())
    if not speech:
        return None
    speaker, time = max(totals.items(), key=lambda kv: kv[1])
    return speaker if time / speech >= share else None


def label_words(words: list[dict[str, Any]], diarization: dict[str, Any], source_id: str,
                local_channel: bool = False) -> list[dict[str, Any]]:
    """Attach `source:speaker` labels. A local microphone with one dominant voice is the local user."""
    turns = diarization.get("segments", [])
    if local_channel and (dominant_speaker(diarization) is not None or not turns):
        return [{**w, "speaker_id": f"{source_id}:{LOCAL_SPEAKER}"} for w in words]
    labelled = []
    for word in words:
        # Diarization drops short pauses and edges; a word up to 2 s from a speaker's turn is theirs.
        speaker = speaker_at(word, turns, tolerance_ms=2000)
        labelled.append({**word, "speaker_id": f"{source_id}:{speaker}" if speaker else None})
    return labelled


def _is_interjection(words: list[dict[str, Any]]) -> bool:
    return (len(words) <= INTERJECTION_MAX_WORDS and
            words[-1]["end_ms"] - words[0]["start_ms"] <= INTERJECTION_MAX_MS)


def _runs(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group each speaker's own words by their own pauses.

    Speech from another channel or speaker that overlaps in time (an answer, a
    «vale», residual echo) never cuts a speaker's turn; it becomes its own turn.
    """
    by_speaker: dict[Any, list[dict[str, Any]]] = {}
    for word in words:
        by_speaker.setdefault(word["speaker_id"], []).append(word)
    runs: list[list[dict[str, Any]]] = []
    for stream in by_speaker.values():
        current: list[dict[str, Any]] = []
        for word in sorted(stream, key=lambda w: (w["start_ms"], w["end_ms"])):
            if current and word["start_ms"] - current[-1]["end_ms"] >= MERGE_GAP_MS:
                runs.append(current)
                current = []
            current.append(word)
        if current:
            runs.append(current)
    return runs


def _split_on_turn_taking(runs: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """A run is cut where another speaker holds the floor (a non-interjection run fully inside its gap)."""
    floor = sorted((r for r in runs if not _is_interjection(r)), key=lambda r: r[0]["start_ms"])
    starts = [r[0]["start_ms"] for r in floor]
    result = []
    for run in runs:
        piece = [run[0]]
        for previous, word in zip(run, run[1:]):
            lo = bisect.bisect_left(starts, previous["end_ms"])
            hi = bisect.bisect_right(starts, word["start_ms"])
            taken = any(other[0]["speaker_id"] != run[0]["speaker_id"] and other[-1]["end_ms"] <= word["start_ms"]
                        for other in floor[lo:hi])
            if taken:
                result.append(piece)
                piece = []
            piece.append(word)
        result.append(piece)
    return result


def _absorb(runs: list[list[dict[str, Any]]]) -> tuple[list[list[dict[str, Any]]], list[list[dict[str, Any]]]]:
    """Short runs that fall inside another speaker's turn are interjections, kept aside."""
    main, asides = [], []
    long_runs = sorted((r for r in runs if not _is_interjection(r)), key=lambda r: r[0]["start_ms"])
    starts = [r[0]["start_ms"] for r in long_runs]
    for run in runs:
        inside = False
        if _is_interjection(run):
            # Candidates start before the interjection; a turn is ≤ 135 s long, so look back that far.
            lo = bisect.bisect_left(starts, run[0]["start_ms"] - int(MAX_TURN_MS * 1.5))
            hi = bisect.bisect_right(starts, run[0]["start_ms"])
            inside = any(other[0]["speaker_id"] != run[0]["speaker_id"] and
                         run[-1]["end_ms"] <= other[-1]["end_ms"] + MERGE_GAP_MS for other in long_runs[lo:hi])
        (asides if inside else main).append(run)
    return main, asides


def _split_long(run: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    pieces, piece = [], []
    for word in run:
        if piece and (word["start_ms"] - piece[-1]["end_ms"] >= SPLIT_GAP_MS or
                      (word["end_ms"] - piece[0]["start_ms"] > MAX_TURN_MS and SENTENCE_END.search(piece[-1]["text"])) or
                      word["end_ms"] - piece[0]["start_ms"] > MAX_TURN_MS * 1.5):
            pieces.append(piece)
            piece = []
        piece.append(word)
    if piece:
        pieces.append(piece)
    return pieces


def build_turns(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Words from every source (labelled) → ordered turns with `t000000` ids."""
    ordered = sorted(words, key=lambda w: (w["start_ms"], w["end_ms"], w.get("source_id") or ""))
    runs, asides = _absorb(_split_on_turn_taking(_runs(ordered)))
    aside_ids = {id(a) for a in asides}
    pieces = [piece for run in runs for piece in _split_long(run)] + asides
    turns = []
    for piece in sorted(pieces, key=lambda p: (p[0]["start_ms"], p[-1]["end_ms"])):
        turns.append({"text": " ".join(w["text"] for w in piece), "start_ms": piece[0]["start_ms"],
                      "end_ms": max(w["end_ms"] for w in piece), "speaker_id": piece[0]["speaker_id"],
                      "source_id": piece[0].get("source_id"), "word_count": len(piece),
                      "interjection": id(piece) in aside_ids, "members": piece})
    for index, turn in enumerate(turns):
        turn["id"] = f"t{index:06d}"
    return turns
