# Voice recognition evaluation

Profile enrollment, diarization and identification are distinct operations.
Diarization assigns anonymous speaker labels. Identification compares a local
speaker centroid to enrolled profiles using cosine similarity and a margin over
the second candidate; multiple clean crops must support a candidate.
Audio source (microphone/system) alone never establishes employee identity.
Profiles and samples must use the same pinned model revision. Short, overlapping,
unstable or incompatible evidence causes abstention.

The current threshold and margin are pilot settings, not calibrated production
metrics. Candidates remain provisional and are not inserted as confirmed employee
IDs in the exported transcript. Do not use enrollment recordings as your test set.

`synth/speakers/evaluation.py` accepts a private JSON dataset with `kind: held_out`,
`profiles` (id, employee_id, normalized 256-dimensional vector) and `trials`
(id, device, condition, independent_of_enrollment, expected_employee_id, vectors).
Unknown speakers have expected_employee_id null. Run it locally:

```sh
python -m synth.speakers.evaluation private-dataset.json --output metrics.json
```

Enroll at least three consenting people and test recordings made separately from
enrollment, plus an unknown speaker. Include microphone differences, remote-call
codecs, silence, overlap and changed rooms. Record false candidates, missed enrolled
speakers, coverage and abstentions. Have an owner review thresholds before any
change to confirmed-identity policy. Keep datasets and embeddings out of Git.
The report contains aggregate metrics and no vectors. The public tests use
orthogonal artificial vectors only; they demonstrate evaluator behavior and do
not establish acoustic accuracy or performance between physical Macs.
