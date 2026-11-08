"""Reconcile hypotheses of the SAME source audio across overlapping windows.

This is word ownership/deduplication, never independent speaker evidence or a
claim that two agreeing ASR hypotheses prove the words correct. Missing/conflicting
words remain reviewable; a time jitter around a cut cannot discard both copies.
"""
import re
import unicodedata

POLICY = 'temporal_lcs_shared_ownership_review_v2'
MAX_CENTER_DELTA_MS = 500


def key(word):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFC', word['text']).casefold()))


def validate(words, bounds):
    previous = -1
    if len(words) > 512:
        raise ValueError('seam_too_many_words')
    for word in words:
        start, end = word.get('start_ms'), word.get('end_ms')
        if (type(start) is not int or type(end) is not int or not bounds[0] <= start <= end <= bounds[1]
                or start < previous or not isinstance(word.get('text'), str) or not word['text'].strip()):
            raise ValueError('seam_word_invalid')
        previous = start


def _match(left, right):
    return key(left) == key(right) and abs(left['start_ms']+left['end_ms']-right['start_ms']-right['end_ms']) <= 2*MAX_CENTER_DELTA_MS


def resolve_cut(left, right, boundary_ms):
    """Keep the ordinary core hypotheses; fix only shared word ownership.

    Unioning the entire overlap promoted clipped context in the public pilot.
    Here an existing one-sided assignment stays unchanged. Matched words that
    would be selected twice or zero times receive exactly one shared owner.
    Unmatched context words remain explicit review data, never fabricated speech.
    """
    if type(boundary_ms) is not int or not 0 <= boundary_ms <= 86400000:
        raise ValueError('seam_boundary_invalid')
    validate(left, (0, 86400000)); validate(right, (0, 86400000))
    def center(word):return (word['start_ms']+word['end_ms'])/2
    a=[(i,w) for i,w in enumerate(left) if abs(center(w)-boundary_ms)<=MAX_CENTER_DELTA_MS]
    b=[(i,w) for i,w in enumerate(right) if abs(center(w)-boundary_ms)<=MAX_CENTER_DELTA_MS]
    n,m=len(a),len(b)
    dp=[[(0,0)]*(m+1) for _ in range(n+1)]
    for i in range(n-1,-1,-1):
        for j in range(m-1,-1,-1):
            best=max(dp[i+1][j],dp[i][j+1])
            if _match(a[i][1],b[j][1]):
                score=dp[i+1][j+1]
                best=max(best,(score[0]+1,score[1]-abs(center(a[i][1])-center(b[j][1]))))
            dp[i][j]=best
    pairs=[];i=j=0
    while i<n and j<m:
        score=dp[i+1][j+1]
        candidate=(score[0]+1,score[1]-abs(center(a[i][1])-center(b[j][1])))
        if _match(a[i][1],b[j][1]) and dp[i][j]==candidate:
            pairs.append((a[i][0],b[j][0]));i+=1;j+=1
        elif dp[i][j]==dp[i+1][j]:i+=1
        else:j+=1
    owned_left={i:dict(w) for i,w in enumerate(left) if center(w)<boundary_ms}
    owned_right={i:dict(w) for i,w in enumerate(right) if center(w)>=boundary_ms}
    fixes=[]
    for ai,bj in pairs:
        selected=int(ai in owned_left)+int(bj in owned_right)
        if selected!=1:
            owned_left.pop(ai,None);owned_right.pop(bj,None)
            fixes.append({**left[ai],'start_ms':min(left[ai]['start_ms'],right[bj]['start_ms']),
                          'end_ms':max(left[ai]['end_ms'],right[bj]['end_ms'])})
    matched_a={i for i,_ in pairs};matched_b={j for _,j in pairs}
    reviews=[]
    for side,candidates,matched in [('left',a,matched_a),('right',b,matched_b)]:
        for index,w in candidates:
            if index not in matched:
                selected = owned_left if side=='left' else owned_right
                reviews.append({**w,'window':side,'reason':'seam_unmatched_hypothesis','selected':index in selected})
                if index in selected:
                    selected[index] = {**selected[index], 'evidence_state':'needs_review',
                                       'review_reason':'seam_disagreement'}
    words=[*owned_left.values(),*owned_right.values(),*fixes]
    words.sort(key=lambda w:(w['start_ms'],w['end_ms']))
    return words,reviews
