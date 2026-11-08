"""Session-only voice references with auditable, non-overlapping lineage.

Inputs are authenticated local observations, not API data. Rebuild with the current
permitted catalog on each read, so no cached inferred name survives revocation.
A fallback never trains a reference and never becomes a confirmed identity.
"""
from .matching_legacy import classify, PILOT_THRESHOLD, PILOT_MARGIN
from .matching import suggest, THRESHOLDS
from .profiles import normalized
from synth.worker.voice_binding import overlap

POLICY = 'independent_rooted_crop10_references_v3'


def strong_vote(vector, profiles):
    scores = {}
    for profile in profiles:
        employee = profile['employee_id']
        score = sum(a*b for a,b in zip(vector, profile['vector']))
        scores[employee] = max(score, scores.get(employee, -1))
    ranked = sorted(scores, key=lambda employee: -scores[employee])
    if not ranked:
        return None
    best = ranked[0]
    margin = scores[best] - (scores[ranked[1]] if len(ranked) > 1 else -1)
    return best if scores[best] >= PILOT_THRESHOLD and margin >= PILOT_MARGIN else None


def pair(observations, model):
    """A ten-second observation has exactly the two five-second parents."""
    five = [o for o in observations if o['model'] == model and o['crop_seconds'] == 5]
    ten = [o for o in observations if o['model'] == model+'#crop10s' and o['crop_seconds'] == 10]
    if len(five) != 2:
        return [], None
    five.sort(key=lambda o: o['ranges'][0][0])
    if overlap(five[0]['ranges'], five[1]['ranges']):
        return [], None
    fields = ('capture_id', 'owner', 'source', 'model_lock_sha256', 'policy')
    if any(five[0][k] != five[1][k] for k in fields):
        return [], None
    ranges = [r for o in five for r in o['ranges']]
    if ranges != sorted(ranges):
        return [], None
    joined = next((o for o in ten if o['ranges'] == ranges and all(o[k] == five[0][k] for k in fields)), None)
    return five, joined


class ReferenceSession:
    def __init__(self, profiles, model, identities=None):
        self.model = model
        self.profiles = [{**p, 'vector': normalized(p['vector'], 128)} for p in profiles if p['model'] == model]
        self.references = {}
        self.pending_crop10 = {}
        self.capture = None
        self.identities = {p['id']: {k: p.get(k) for k in ('id', 'employee_id', 'name', 'model')}
                           for p in (identities if identities is not None else profiles)
                           if p['model'] in (model, model+'#crop10s')}

    def reference_pool(self):
        return [sample for root in self.references.values() for sample in [root,*root.get('additional',[])]]

    def observe_crop10(self, group_id, observations, profiles):
        """Two independent direct matches can calibrate this capture locally.

        The supplied catalog is current and already approved. Session fallback
        vectors are never accepted here, nor are new shared samples exported.
        """
        five,ten = pair(observations,self.model)
        if ten is None:
            return
        capture = (five[0]['capture_id'],five[0]['owner'],five[0]['model_lock_sha256'])
        if self.capture is not None and capture != self.capture:
            raise ValueError('voice_session_capture_mismatch')
        self.capture = capture
        # Keep the existing, separate crop10 operating point. The legacy PLDA
        # cutoff belongs to five-second vectors, not raw ten-second embeddings.
        # This calibrates local suggestions only; shared export keeps its own
        # stricter independent-root checks in learning.samples_from_contexts.
        direct = suggest(normalized(ten['embedding']),10,profiles,
                         thresholds={**THRESHOLDS,'low':THRESHOLDS['high']})
        person = direct.get('candidate_employee_id')
        if direct['state'] != 'suggested' or not person or direct['profile_id'] not in self.identities:
            return
        if any((vote:=strong_vote(normalized(o['rho128'],128),self.profiles)) is not None and vote!=person for o in five):
            return
        pending = self.pending_crop10.setdefault(person,[])
        if any(overlap(p['ranges'],ten['ranges']) for p in pending):
            return
        anchor = {'id':'session:'+person+':'+group_id,'employee_id':person,
            'name':self.identities[direct['profile_id']]['name'],'enrolled_profile_id':direct['profile_id'],
            'model':self.model+'#crop10s','vector':normalized(ten['embedding']),
            'ranges':ten['ranges'],'evidence_ids':[o['id'] for o in five]+[ten['id']],
            'provenance':'approved_crop10_independent_votes'}
        if len(pending)<4:
            pending.append(anchor)
        if len(pending)<2:
            return
        for sample in pending:
            existing = self.references.get(person)
            pool = [existing,*existing.get('additional',[])] if existing else []
            if len(pool)>=4 or any(overlap(p['ranges'],sample['ranges']) for p in pool):
                continue
            if existing:
                existing.setdefault('additional',[]).append(sample)
            else:
                self.references[person]=dict(sample)

    def confirm(self, group_id, observations, profile_id, decision_id):
        five, ten = pair(observations, self.model)
        profile = self.identities.get(profile_id)
        if profile is None or ten is None:
            raise ValueError('voice_confirmation_evidence_unavailable')
        capture = (five[0]['capture_id'], five[0]['owner'], five[0]['model_lock_sha256'])
        if self.capture is not None and self.capture != capture:
            raise ValueError('voice_session_capture_mismatch')
        self.capture = capture
        ids = [o['id'] for o in five]+[ten['id']]
        self.pending_crop10 = {person:[sample for sample in pending
            if not overlap(sample['ranges'],ten['ranges'])] for person,pending in self.pending_crop10.items()}
        # A correction also invalidates secondary prototypes and echoed copies.
        retained = {}
        for employee, root in self.references.items():
            samples = [r for r in [root,*root.get('additional',[])]
                       if not set(r['evidence_ids']) & set(ids)
                       and not overlap(r['ranges'], ten['ranges'])]
            if samples:
                primary = {k:v for k,v in samples[0].items() if k != 'additional'}
                primary['additional'] = samples[1:]
                retained[employee] = primary
        self.references = retained
        self.references[profile['employee_id']] = {'id': 'session:'+profile['employee_id'],
            'employee_id': profile['employee_id'], 'name': profile['name'], 'enrolled_profile_id': profile_id,
            'model': self.model+'#crop10s', 'vector': normalized(ten['embedding']),
            'ranges': [r for o in five for r in o['ranges']], 'evidence_ids': ids,
            'provenance': 'human_confirmation', 'decision_id': decision_id}
        return {'group_id': group_id, 'state': 'confirmed', 'reason': 'human_confirmation', 'confirmed': True,
            'candidate_employee_id': profile['employee_id'], 'candidate_name': profile['name'],
            'employee_id': profile['employee_id'], 'profile_id': profile_id, 'policy': POLICY,
            'evidence_ids': ids, 'reference_evidence_ids': [], 'confirmation_id': decision_id}

    def observe(self, group_id, observations, *, competing_candidate=None):
        unknown = {'state': 'unknown', 'reason': 'short_or_unclean', 'candidate_employee_id': None,
            'candidate_name': None, 'profile_id': None, 'employee_id': None, 'policy': POLICY,
            'group_id': group_id, 'confirmed': False, 'evidence_ids': [], 'reference_evidence_ids': []}
        five, ten = pair(observations, self.model)
        if not five:
            return unknown
        capture = (five[0]['capture_id'], five[0]['owner'], five[0]['model_lock_sha256'])
        if self.capture is None:
            self.capture = capture
        elif self.capture != capture:
            raise ValueError('voice_session_capture_mismatch')
        vectors = [normalized(o['rho128'], 128) for o in five]
        direct = classify(vectors, self.profiles, 128)
        direct_id = direct.get('candidate_employee_id')
        strong = {employee for v in vectors if (employee := strong_vote(v, self.profiles)) is not None}
        ranges = [r for o in five for r in o['ranges']]
        # Echoes, revised decodes or reused crops cannot recognize themselves.
        references = [r for r in self.reference_pool() if not overlap(r['ranges'], ranges)]
        fallback = suggest(normalized(ten['embedding']) if ten is not None else None, 10 if ten else 0, references)
        proposed = fallback.get('candidate_employee_id') if fallback['state'] == 'suggested' else None
        if proposed is not None and strong-{proposed}:
            proposed = None
        if direct_id is not None and strong-{direct_id}:
            direct_id = None
            proposed = None
        candidate = direct_id or proposed
        if candidate and competing_candidate and candidate != competing_candidate:
            return {**unknown, 'reason':'crop_policy_disagreement',
                    'evidence_ids':[o['id'] for o in five]+([ten['id']] if ten is not None else [])}
        outcome = {**unknown, 'reason': 'legacy_evidence' if direct_id else 'session_reference' if proposed else 'insufficient_or_conflicting_evidence',
            'evidence_ids': [o['id'] for o in five]+([ten['id']] if ten is not None else [])}
        if candidate:
            # Return the enrolled profile ID, never an ephemeral session ID.
            profile = self.identities[direct['profile_id'] if direct_id else self.references[proposed]['enrolled_profile_id']]
            outcome.update(state='suggested', candidate_employee_id=candidate, candidate_name=profile.get('name'),
                profile_id=profile['id'], similarity=direct.get('similarity') if direct_id else fallback['similarity'],
                margin=direct.get('margin') if direct_id else fallback['margin'])
            if proposed and not direct_id:
                anchor = next(r for r in references if r['id']==fallback['profile_id'])
                outcome['profile_id'] = anchor['enrolled_profile_id']
                outcome['reference_evidence_ids'] = anchor['evidence_ids']
        # Only independently corroborated enrollment votes add a reference.
        # Fallback suggestions cannot reproduce themselves or accumulate drift.
        if direct_id and ten is not None:
            existing = self.references.get(direct_id)
            samples = [existing,*existing.get('additional',[])] if existing else []
            if len(samples)>=4 or any(overlap(r['ranges'],ranges) for r in samples):
                return outcome
            anchor = {'id': 'session:'+direct_id+':'+group_id, 'employee_id': direct_id,
                'name': outcome['candidate_name'], 'enrolled_profile_id': outcome['profile_id'],
                'model': self.model+'#crop10s', 'vector': normalized(ten['embedding']), 'ranges': ranges,
                'evidence_ids': [o['id'] for o in five]+[ten['id']], 'provenance':'enrolled_high_confidence'}
            if existing:
                existing.setdefault('additional',[]).append(anchor)
            else:
                self.references[direct_id] = anchor
        return outcome
