"""Carry only explicit human voice decisions into the final text, without vectors."""
import copy
import hashlib


def coverage(spans, start, end):
    selected = sorted((max(a,start),min(b,end)) for a,b in spans if max(a,start)<min(b,end))
    total, previous = 0, start
    for a,b in selected:
        total += max(0,b-max(a,previous))
        previous = max(previous,b)
    return total


def confirmed_identity(hits, start, end):
    """Adjacent human decisions can cover a phrase; overlap stays uncertain."""
    if not hits or end <= start:
        return None
    first = hits[0]
    employee = first.get('candidate_employee_id')
    if not employee or not first.get('candidate_name') or any(
            not match.get('confirmed') or match.get('candidate_employee_id') != employee for match in hits):
        return None
    for index, match in enumerate(hits):
        for other in hits[index+1:]:
            if any(max(a,c,start) < min(b,d,end)
                   for a,b in match['speech_spans_ms'] for c,d in other['speech_spans_ms']):
                return None
    spans = [span for match in hits for span in match['speech_spans_ms']]
    return first if coverage(spans,start,end) >= .95*(end-start) else None


def map_confirmed(canonical, status):
    """A name covers only complete, unambiguous speech reviewed by the owner."""
    result = copy.deepcopy(canonical)
    matches = status.get('matches', [])
    mapped, actions = {}, {}
    for segment in result['segments']:
        start,end = segment['start_ms'],segment['end_ms']
        hits = [m for m in matches if m['source']==segment.get('source_id')
                and coverage(m['speech_spans_ms'],start,end)>0]
        match = confirmed_identity(hits,start,end)
        if match is None:
            continue
        employee = match['candidate_employee_id']
        identifier = 'live-confirmed:'+hashlib.sha256((status['capture_id']+':'+employee).encode()).hexdigest()[:24]
        # The upload is still anonymous. The owner's separate action applies the name.
        segment['speaker_id'],segment['employee_id'] = identifier,None
        mapped[segment['id']] = identifier
        actions[identifier] = {'action':'confirm','speaker_id':identifier,
                              'name':match['candidate_name'],'employee_id':employee}
    segments = {s['id']:s for s in result['segments']}
    for turn in result.get('turns') or []:
        ids = turn.get('segment_ids') or []
        if any(identifier in mapped for identifier in ids):
            labels = {segments[identifier].get('speaker_id') for identifier in ids}
            turn['speaker_id'] = next(iter(labels)) if len(labels)==1 else None
            turn['employee_id'] = None
    return result,list(actions.values())


def export_confirmations(canonical, task, actor, api, state, capture_root, appdata):
    capture_id = task['metadata'].get('capture_id')
    if not capture_id:
        return canonical,[]
    path = state/'incremental'/capture_id/'voice-decisions.sqlite3'
    if not path.exists():
        return canonical,[]
    from .capture_decisions import CaptureDecisions
    mode = getattr(api,'auth_mode',None)
    identity = actor['id'] if mode != 'keys' else 'keys:'+hashlib.sha256((appdata/'desktop-api-key').read_text().strip().encode()).hexdigest()
    with CaptureDecisions(state,capture_id,identity) as decisions:
        _,rows = decisions.seal()
    if not rows:
        return canonical,[]
    from .live_status import capture_status
    status = capture_status(capture_id,identity,actor,state,capture_root,_seal=True)
    return map_confirmed(canonical,status)
