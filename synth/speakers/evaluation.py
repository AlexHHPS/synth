"""Offline evaluation of held-out speaker vectors; never certifies identity."""
import math
from .matching import classify


def vector(value):
    if not isinstance(value, list) or len(value) != 256:
        raise ValueError('expected_256_dimensions')
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value):
        raise ValueError('invalid_vector')
    length = math.sqrt(sum(x*x for x in value))
    if length == 0:
        raise ValueError('zero_vector')
    return [x/length for x in value]


def evaluate(dataset):
    if dataset.get('kind') not in ('synthetic', 'held_out'):
        raise ValueError('dataset_kind_required')
    profiles = dataset.get('profiles', [])
    trials = dataset.get('trials', [])
    if not profiles or not trials:
        raise ValueError('empty_dataset')
    ids = set()
    enrolled = set()
    clean = []
    for p in profiles:
        if not p.get('id') or not p.get('employee_id') or p['id'] in ids:
            raise ValueError('invalid_profile_identity')
        ids.add(p['id']); enrolled.add(p['employee_id'])
        clean.append({**p, 'vector': vector(p['vector'])})
    counts = dict(total=0, known=0, unknown=0, candidates=0, correct_candidates=0,
                  false_positive=0, false_negative=0, abstentions=0)
    seen = set()
    for t in trials:
        if not t.get('id') or t['id'] in seen or not t.get('device') or not t.get('condition'):
            raise ValueError('invalid_trial_metadata')
        seen.add(t['id'])
        if 'expected_employee_id' not in t or not t.get('independent_of_enrollment'):
            raise ValueError('held_out_ground_truth_required')
        expected = t['expected_employee_id']
        if expected is not None and expected not in enrolled:
            raise ValueError('ground_truth_not_enrolled')
        outcome = classify([vector(v) for v in t['vectors']], clean)
        candidate = outcome.get('candidate_employee_id') if outcome['state'] == 'candidate' else None
        counts['total'] += 1
        counts['known' if expected is not None else 'unknown'] += 1
        if candidate is None:
            counts['abstentions'] += 1
        else:
            counts['candidates'] += 1
            counts['correct_candidates' if candidate == expected else 'false_positive'] += 1
        if expected is not None and candidate != expected:
            counts['false_negative'] += 1
    return {'kind': dataset['kind'], 'counts': counts,
            'coverage': counts['candidates']/counts['total'],
            'false_positive_rate': counts['false_positive']/counts['total'],
            'false_negative_rate_known': counts['false_negative']/counts['known'] if counts['known'] else None,
            'production_calibration': 'pending', 'confirms_identity': False}


if __name__ == '__main__':
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description='Evaluate held-out local speaker embeddings, without exporting vectors.')
    parser.add_argument('dataset', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = evaluate(json.loads(args.dataset.read_text()))
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print('Evaluation saved; production calibration remains pending.')
