"""Guard against catastrophic batch repetition; keep live text with provenance.

This is a quality warning, not a proof that a repeated phrase is hallucinated.
No words are invented or removed. A fallback has unknown source speakers.
"""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from synth.contracts.transcript import Transcript


def repetition_report(transcripts):
    result = {}
    for source, transcript in transcripts.items():
        phrases = [' '.join(s['text'].casefold().split()) for s in transcript['segments']]
        dominant = max(Counter(phrases).values(), default=0)
        ratio = dominant / len(phrases) if phrases else 0
        result[source] = {'segments': len(phrases), 'dominant_phrase_fraction': round(ratio, 4),
                          'suspicious': len(phrases) >= 20 and ratio >= .8}
    return result


def live_transcript(document, duration_ms, content_hash):
    segments = []
    for item in document['segments']:
        start, end = item['audio_start_time'], item['audio_end_time']
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (start, end)):
            raise ValueError('live_transcript_invalid')
        # Validate before rounding: never clamp a corrupt timeline into validity.
        if start < 0 or end < start or end * 1000 > duration_ms:
            raise ValueError('live_transcript_invalid')
        text = item['text']
        if not isinstance(text, str) or not text.strip():
            raise ValueError('live_transcript_invalid')
        segments.append({'id': f's{len(segments):06d}', 'start_ms': round(start * 1000),
                         'end_ms': round(end * 1000), 'text': text,
                         'source_id': 'live_mix', 'speaker_id': None, 'employee_id': None})
    return Transcript.model_validate({'language': 'es', 'duration_ms': duration_ms,
        'model_fingerprint': 'capture-live:unknown-model:' + content_hash,
        'segments': segments}).model_dump()


def load_closed_live(task, capture_root, duration_ms):
    root = Path(capture_root).resolve(strict=True)
    sources = [Path(d['path']) for d in task['metadata']['sources'].values()]
    parents = {p.parent for p in sources}
    if len(parents) != 1:
        raise ValueError('live_capture_receipt_invalid')
    parent = parents.pop()
    if parent.name != 'sources' or parent.is_symlink():
        raise ValueError('live_capture_receipt_invalid')
    receipt, live = parent / 'capture.json', parent.parent / 'transcripts.json'
    for p in (receipt, live):
        if p.is_symlink() or not p.resolve(strict=True).is_relative_to(root):
            raise ValueError('live_capture_path_denied')
    closed = json.loads(receipt.read_text())
    capture_id = task['metadata'].get('capture_id')
    if not capture_id or closed.get('state') != 'closed' or closed.get('capture_id') != capture_id:
        raise ValueError('live_capture_receipt_invalid')
    if live.stat().st_size > 5_000_000:
        raise ValueError('live_transcript_too_large')
    content = live.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    document = json.loads(content)
    converted = live_transcript(document, duration_ms, digest)
    if repetition_report({'live': converted})['live']['suspicious']:
        raise ValueError('live_transcript_repetitive')
    return converted, digest


def select_canonical(batch, canonical, task, capture_root):
    report = {'batch_sources': repetition_report(batch), 'selected': 'batch'}
    used_sources = {segment.get('source_id') for segment in canonical['segments']}
    if not any(s['suspicious'] and source in used_sources
               for source, s in report['batch_sources'].items()):
        return canonical, report
    try:
        recovered, digest = load_closed_live(task, capture_root, canonical['duration_ms'])
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('batch_transcription_repetitive') from None
    report.update(selected='capture_live', live_sha256=digest, speakers='unknown',
                  reason='batch_extreme_repetition', model='unrecorded_live_model')
    return recovered, report
