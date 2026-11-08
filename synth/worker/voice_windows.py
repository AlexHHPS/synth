"""Pure selection of disjoint guarded voice crops, shared by live and final analysis."""
from array import array

RATE = 16000
WINDOW = 5 * RATE
GUARD = 120 * 16


def merge(intervals):
    result = []
    for start, end in sorted(intervals):
        if start >= end:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def clean_intervals(turns, speaker, frames):
    own, others = [], []
    for turn in turns:
        start, end = turn['start_ms'], turn['end_ms']
        if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int)
                or not isinstance(end, int) or not 0 <= start < end):
            raise ValueError('voice_turn_invalid')
        interval = (min(frames, start * 16), min(frames, end * 16))
        (own if turn['speaker_id'] == speaker else others).append(interval)
    own = merge(own)
    blocked = merge((max(0, start-GUARD), min(frames, end+GUARD)) for start, end in others)
    clean = []
    for start, end in own:
        cursor, stop = start+GUARD, end-GUARD
        for left, right in blocked:
            if right <= cursor:
                continue
            if left >= stop:
                break
            if left > cursor:
                clean.append((cursor, min(left, stop)))
            cursor = max(cursor, right)
        if cursor < stop:
            clean.append((cursor, stop))
    overlap = sum(max(0, min(end, right)-max(start, left))
                  for start, end in own for left, right in merge(others))
    return clean, overlap


def spaced_offsets(frames, count):
    count = min(count, frames // WINDOW)
    if count <= 0:
        return []
    if count == 1:
        return [(frames-WINDOW)//2]
    return [i*(frames-WINDOW)//(count-1) for i in range(count)]


def voice_windows(audio, turns, speaker, minimum=2, *, maximum=3):
    if type(maximum) is not int or not minimum <= maximum <= 12:
        raise ValueError('voice_window_limit_invalid')
    intervals, overlap = clean_intervals(turns, speaker, len(audio))
    total = sum(end-start for start, end in intervals)
    report = {'clean_seconds': round(total/RATE, 3), 'overlap_seconds': round(overlap/RATE, 3),
              'boundary_guard_ms': GUARD//16, 'window_policy': 'disjoint_5s_v1'}
    if total < minimum*WINDOW:
        return [], report
    contiguous = [(start+offset, start+offset+WINDOW) for start, end in intervals
                  for offset in spaced_offsets(end-start, maximum)]
    if len(contiguous) >= minimum:
        indices = [0] if len(contiguous) == 1 else sorted({i*(len(contiguous)-1)//(min(maximum, len(contiguous))-1) for i in range(min(maximum, len(contiguous)))})
        selected = [[contiguous[i]] for i in indices]
    else:
        # Short turns can still supply independent evidence. Preserve the source
        # ranges for every join so no sample can cast a second vote.
        selected = []
        for offset in spaced_offsets(total, maximum):
            spans, cursor = [], 0
            for start, end in intervals:
                left, right = max(offset-cursor, 0), min(offset+WINDOW-cursor, end-start)
                if left < right:
                    spans.append((start+left, start+right))
                cursor += end-start
            selected.append(spans)
    windows = []
    for spans in selected:
        chunk = array('h')
        for start, end in spans:
            chunk.extend(audio[start:end])
        windows.append((chunk, {'source_ranges_ms': [[start/16, end/16] for start, end in spans],
                                'joined_turns': len(spans) > 1}))
    return windows, report

def paired_voice_windows(audio, turns, speaker):
    """One 10 s clean reference split into its exact two disjoint 5 s parents.

    Prefer one contiguous interval. If speech is fragmented, preserve every join.
    Spreading separate 5 s crops over 30 s can miss the first reliable anchor.
    """
    intervals, overlap = clean_intervals(turns, speaker, len(audio))
    total = sum(end-start for start,end in intervals)
    report = {'clean_seconds': round(total/RATE, 3), 'overlap_seconds': round(overlap/RATE, 3),
              'boundary_guard_ms': GUARD//16, 'window_policy': 'first_clean_10s_two_parents_v1'}
    if total < 2*WINDOW:
        return [], report
    contiguous = next(((start, start+2*WINDOW) for start,end in intervals if end-start >= 2*WINDOW), None)
    selected, remaining = [], 2*WINDOW
    for start,end in ([contiguous] if contiguous is not None else intervals):
        stop = min(end,start+remaining)
        selected.append((start,stop))
        remaining -= stop-start
        if not remaining:
            break
    windows = []
    for offset in (0,WINDOW):
        spans, cursor = [], 0
        for start,end in selected:
            left,right = max(offset-cursor,0),min(offset+WINDOW-cursor,end-start)
            if left < right:
                spans.append((start+left,start+right))
            cursor += end-start
        chunk = array('h')
        for start,end in spans:
            chunk.extend(audio[start:end])
        windows.append((chunk, {'source_ranges_ms':[[start/16,end/16] for start,end in spans],
                               'joined_turns':len(spans)>1}))
    return windows, report
