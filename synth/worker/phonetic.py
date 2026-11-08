"""Spanish phonetic keys and similarity for glossary candidates (no model, deterministic).

`key` folds what Spanish ASR confuses: b/v, ll/y, silent h, c/z/s before e/i,
g/j before e/i, qu/k/c hard, gu→g, ñ→n, double letters, and vowel nuances
(o/u, e/i, nasal m/n). `similar` combines Jaro-Winkler on the keys and on the
plain spelling so «Baldibar» ~ «Valdivar» and «Luservo» ~ «Lucerno».
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

VOWELS = "aeiou"
COMMON = frozenset(line.strip() for line in (Path(__file__).with_name("data") / "es_common.txt").read_text().splitlines()
                   if line.strip() and not line.startswith("#"))


def looks_like_name(text: str, start: int) -> bool:
    """Proper-noun shape: capitalized and not at the start of a sentence, or not a common word."""
    if not text[start:start + 1].isupper():
        return False
    before = text[:start].rstrip()
    sentence_start = not before or before[-1] in ".?!¿¡:\n"
    return not sentence_start or plain(text[start:].split()[0]) not in COMMON


def plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold().replace("ñ", "n"))
    return re.sub(r"[^a-z0-9 ]", "", "".join(c for c in decomposed if not unicodedata.combining(c)))


def key(word: str) -> str:
    w = plain(word).replace(" ", "")
    rules = [
        (r"ch", "X"), (r"ll", "Y"), (r"qu", "K"), (r"gu(?=[ei])", "G"), (r"c(?=[ei])", "S"),
        (r"g(?=[ei])", "J"), (r"z", "S"), (r"c", "K"), (r"k", "K"), (r"x", "KS"), (r"v", "B"),
        (r"w", "B"), (r"y(?=[aeiou])", "Y"), (r"y", "I"), (r"ph", "F"), (r"h", ""),
        (r"m(?=[bp])", "N"), (r"rr", "R"),
    ]
    for pattern, replacement in rules:
        w = re.sub(pattern, replacement, w)
    w = w.upper()
    w = w.replace("O", "U").replace("E", "I")
    w = re.sub(r"(.)\1+", r"\1", w)
    # Final -m/-n and -s are weak in fast speech.
    return w.rstrip("S") or w


def jaro_winkler(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    window = max(len(a), len(b)) // 2 - 1
    flags_a, flags_b = [False] * len(a), [False] * len(b)
    matches = 0
    for i, ca in enumerate(a):
        for j in range(max(0, i - window), min(len(b), i + window + 1)):
            if not flags_b[j] and b[j] == ca:
                flags_a[i] = flags_b[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    ma = [c for c, f in zip(a, flags_a) if f]
    mb = [c for c, f in zip(b, flags_b) if f]
    transpositions = sum(x != y for x, y in zip(ma, mb)) / 2
    jaro = (matches / len(a) + matches / len(b) + (matches - transpositions) / matches) / 3
    prefix = 0
    for x, y in zip(a, b):
        if x != y or prefix == 4:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


def similarity(heard: str, term: str) -> float:
    """0..1: how plausibly `heard` is an ASR rendering of `term`."""
    return round(max(0.6 * jaro_winkler(key(heard), key(term)) + 0.4 * jaro_winkler(plain(heard), plain(term)),
                     jaro_winkler(key(heard), key(term)) if len(key(term)) >= 5 else 0), 4)


def candidates(text: str, terms: list[dict], threshold: float = 0.85, max_ngram: int = 3,
               lowercase_threshold: float = 0.9) -> list[dict]:
    """Spans of `text` (1–3 words) that sound like a glossary term or alias but are not spelled like it.

    Whisper often writes a misheard name in lowercase («baldibar») or splits it («val divar»). A lowercase span is a
    candidate only when none of its words is common Spanish and it sounds very close to the term; a span may have one
    word more or fewer than the term. The text model still accepts or rejects every candidate."""
    tokens = list(re.finditer(r"[\wÀ-ÿ'-]+", text))
    found = []
    for n in range(max_ngram, 0, -1):
        for i in range(len(tokens) - n + 1):
            start, end = tokens[i].start(), tokens[i + n - 1].end()
            span = text[start:end]
            if len(plain(span).replace(" ", "")) < 4 or (n > 1 and re.search(r"[.,;:?!¿¡]", span)):
                continue  # a name does not cross punctuation
            named = looks_like_name(text, start)
            unknown_words = not any(plain(word) in COMMON for word in span.split())
            best = None
            for term in terms:
                names = [term["term"]] + list(term.get("aliases", []))
                split = len(term["term"].split()) != n
                # A name heard with one word more or fewer («val divar») only when no word of it is common Spanish.
                if abs(len(term["term"].split()) - n) > 1 or (split and not unknown_words):
                    continue
                # A longer span must be the name cut in pieces, not the name plus a neighbouring word.
                if n > len(term["term"].split()) and (any(len(plain(w)) < 3 for w in span.split()) or
                                                      any(similarity(w, term["term"]) >= threshold for w in span.split())):
                    continue
                if plain(span) == plain(term["term"]):
                    best = None
                    break
                alias = any(plain(span) == plain(a) for a in term.get("aliases", []))
                # Common lowercase words are never candidates, not even when an older alias spells them.
                if plain(span) in COMMON and not named:
                    continue
                if not alias and (plain(span) in COMMON or not (named or unknown_words)):
                    continue
                score = 1.0 if alias else max(similarity(span, name) for name in names)
                needed = threshold if (alias or (named and not split)) else lowercase_threshold
                if score >= needed and (best is None or score > best["score"]):
                    best = {"start": start, "end": end, "heard": span, "term": term["term"], "score": score}
            if best and not any(f["start"] < best["end"] and best["start"] < f["end"] for f in found):
                found.append(best)
    return sorted(found, key=lambda f: f["start"])
