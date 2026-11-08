"""Guard against batch loops and sustained cross-source duplication.

Repetition is a quality warning, not a proof that a repeated phrase is
hallucinated. Only whole segments that are a known Whisper silence
hallucination («¡Suscríbete al canal!») are dropped; no words are invented.
A live fallback gets per-track speakers when diarization exists.
"""
from collections import Counter
from bisect import bisect_left, bisect_right
import hashlib
import json
import math
import re
from pathlib import Path
from synth.contracts.transcript import Transcript


HALLUCINATIONS = re.compile(
    r"^\W*(¡?suscr[ií]bete al canal!?|gracias por ver el v[ií]deo\.?|subt[ií]tulos realizados por la comunidad de amara\.org"
    r"|subt[ií]tulos por la comunidad de amara\.org|gracias por ver\.?|amara\.org)\W*$", re.IGNORECASE)


def is_hallucination(text):
    """A whole segment that is a phrase Whisper emits on silence or noise."""
    return bool(HALLUCINATIONS.match(text.strip()))


def normalized_phrase(text):
    return ' '.join(re.findall(r'\w+', text.casefold()))


def repetition_report(transcripts):
    result = {}
    for source, transcript in transcripts.items():
        segments = sorted(transcript['segments'], key=lambda s: s['start_ms'])
        phrases = [normalized_phrase(s['text']) for s in segments]
        dominant = max(Counter(phrases).values(), default=0)
        ratio = dominant / len(phrases) if phrases else 0
        # A local decoding loop can be catastrophic while representing only a
        # small fraction of a long meeting. Ignore short conversational replies.
        runs = []
        start = 0
        for end in range(1, len(segments) + 1):
            if end < len(segments) and phrases[end] == phrases[start] and segments[end]['start_ms'] <= segments[end-1]['end_ms'] + 2000:
                continue
            if end - start >= 5 and len(phrases[start].split()) >= 5 and segments[end-1]['end_ms'] - segments[start]['start_ms'] >= 10000:
                runs.append({'start_ms': segments[start]['start_ms'],
                             'end_ms': segments[end-1]['end_ms'], 'segments': end-start})
            start = end
        result[source] = {'segments': len(phrases), 'dominant_phrase_fraction': round(ratio, 4),
                          'repetition_runs': runs,
                          'suspicious': (len(phrases) >= 20 and ratio >= .8) or bool(runs)}
    return result


def cross_source_report(canonical):
    """Flag sustained duplicates; text agreement alone never proves echo.

    Only long, identical phrases with closely matching times count. We do not
    erase them or infer that microphone voices belong to the local user.
    """
    mic = sorted((s for s in canonical['segments'] if s.get('source_id') == 'microphone'), key=lambda s: s['start_ms'])
    starts = [s['start_ms'] for s in mic]
    system = [s for s in canonical['segments'] if s.get('source_id') == 'system']
    matched = []
    for remote in system:
        phrase = normalized_phrase(remote['text'])
        if len(phrase.split()) < 8:
            continue
        for local in mic[bisect_left(starts, remote['start_ms'] - 750):bisect_right(starts, remote['start_ms'] + 750)]:
            overlap = min(local['end_ms'], remote['end_ms']) - max(local['start_ms'], remote['start_ms'])
            duration = max(local['end_ms'] - local['start_ms'], remote['end_ms'] - remote['start_ms'], 1)
            if overlap / duration >= .8 and normalized_phrase(local['text']) == phrase:
                matched.append((max(local['start_ms'], remote['start_ms']), min(local['end_ms'], remote['end_ms'])))
                break
    # Count union duration, so overlapping segments cannot inflate the evidence.
    covered = 0
    previous_end = -1
    for start, end in sorted(matched):
        covered += max(0, end - max(start, previous_end))
        previous_end = max(previous_end, end)
    fraction = len(matched) / len(system) if system else 0
    return {'matching_segments': len(matched), 'overlap_ms': covered,
            'system_fraction': round(fraction, 4),
            'suspicious': len(matched) >= 5 and covered >= 15000}


def batch_quality_report(batch, canonical):
    return {'batch_sources': repetition_report(batch),
            'cross_source': cross_source_report(canonical), 'selected': 'batch'}


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


def label_live(recovered, diarizations, words=None):
    """Give the live fallback per-track speakers from the batch diarization and build turns.

    Remote meeting: system turns name remote speakers; time where the (echo-free)
    microphone has words is the local user. In-person: microphone diarization.
    """
    from .bleed import drop_echo_words
    from .turns import LOCAL_SPEAKER, build_turns, speaker_at
    diarizations, words = diarizations or {}, words or {}
    system = diarizations.get('system') or {}
    remote = bool(system.get('segments'))
    local_words = words.get('microphone', [])
    if remote and local_words and words.get('system'):
        local_words, _ = drop_echo_words(local_words, words['system'])
    units = []
    for segment in recovered['segments']:
        unit = {'text': segment['text'], 'start_ms': segment['start_ms'], 'end_ms': segment['end_ms'],
                'source_id': 'live_mix'}
        speaker = None
        if remote:
            local = sum(max(0, min(w['end_ms'], segment['end_ms']) - max(w['start_ms'], segment['start_ms']))
                        for w in local_words)
            remote_speaker = speaker_at(unit, system['segments'], tolerance_ms=0)
            span = max(1, segment['end_ms'] - segment['start_ms'])
            if local / span >= 0.5 or (local and remote_speaker is None):
                speaker = 'microphone:' + LOCAL_SPEAKER
            elif remote_speaker:
                speaker = 'system:' + remote_speaker
        elif (diarizations.get('microphone') or {}).get('segments'):
            found = speaker_at(unit, diarizations['microphone']['segments'], tolerance_ms=0)
            speaker = 'microphone:' + found if found else None
        segment['speaker_id'] = speaker
        units.append({**unit, 'speaker_id': speaker, 'segment_id': segment['id']})
    if any(u['speaker_id'] for u in units):
        turns = build_turns(units)
        for turn in turns:
            turn['segment_ids'] = [u['segment_id'] for u in turn.pop('members')]
            turn['employee_id'] = None
        recovered['turns'] = turns
    return Transcript.model_validate(recovered).model_dump()


def select_canonical(batch, canonical, task, capture_root, diarizations=None, words=None, allow_live_fallback=True):
    report = batch_quality_report(batch, canonical)
    used_sources = {segment.get('source_id') for segment in canonical['segments']}
    repetitive = any(s['suspicious'] and source in used_sources
                     for source, s in report['batch_sources'].items())
    duplicated = report['cross_source']['suspicious']
    if not repetitive and not duplicated:
        return canonical, report
    if not allow_live_fallback:
        # Revisioned per-source ASR already is the live result. A legacy mixed
        # snapshot would discard its uncertainty and original hypotheses.
        raise ValueError('batch_transcription_repetitive' if repetitive else 'batch_cross_source_duplication')
    try:
        recovered, digest = load_closed_live(task, capture_root, canonical['duration_ms'])
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('batch_transcription_repetitive' if repetitive else 'batch_cross_source_duplication') from None
    recovered = label_live(recovered, diarizations, words)
    labelled = any(segment['speaker_id'] for segment in recovered['segments'])
    report.update(selected='capture_live', live_sha256=digest, speakers='per_track' if labelled else 'unknown',
                  reason='batch_extreme_repetition' if repetitive else 'batch_cross_source_duplication', model='unrecorded_live_model')
    return recovered, report
