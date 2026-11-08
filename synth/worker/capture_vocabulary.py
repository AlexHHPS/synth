"""Approved terms only, bound to one local capture and its authenticated owner.

No agenda, historical transcript or attendee names become an ASR reference.
The immutable per-capture copy preserves the inference policy across restarts.
"""
import hashlib
import json
from pathlib import Path
import time

from .consolidated_capture import atomic_json
from .live_voice import load_owner


def vocabulary(terms):
    if not isinstance(terms, list) or len(terms) > 40:
        raise ValueError('qwen_vocabulary_invalid')
    result = []
    for term in terms:
        if not isinstance(term, str) or not 1 <= len(term) <= 60 or any(ord(c) < 32 for c in term):
            raise ValueError('qwen_vocabulary_invalid')
        if term.casefold() not in {t.casefold() for t in result}:
            result.append(term)
    return result


def prompt(terms):
    terms = vocabulary(terms)
    return ('Vocabulario de esta reunión: ' + ', '.join(terms) + '.') if terms else None


def fingerprint(terms):
    return hashlib.sha256(json.dumps(vocabulary(terms), ensure_ascii=False).encode()).hexdigest()


def load(state, folder, capture_id):
    folder, state = Path(folder), Path(state)
    if not (folder/'capture-owner.json').exists():
        return []  # Older captures have no identity and cannot inherit private vocabulary.
    owner = load_owner(folder, capture_id)
    path = folder/'asr-vocabulary.json'
    if path.exists():
        if path.is_symlink() or path.stat().st_size > 32768:
            raise ValueError('qwen_vocabulary_invalid')
        value = json.loads(path.read_text())
        if value['owner'] != owner or value['capture_id'] != capture_id:
            raise ValueError('qwen_vocabulary_owner_mismatch')
        return vocabulary(value['terms'])
    terms = []
    pending = state/'prepared-capture-context.json'
    if pending.is_file() and not pending.is_symlink() and pending.stat().st_size <= 32768:
        value = json.loads(pending.read_text())
        if value.get('owner') == owner and 0 <= time.time()-value.get('created_at', 0) <= 120:
            terms = vocabulary(value['terms'])
    atomic_json(path, {'owner': owner, 'capture_id': capture_id, 'terms': terms})
    return terms
