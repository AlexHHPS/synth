"""Meeting/profile comparison kept on this Mac.

Suggestions are not identities: a speaker is named only after the owner
confirms it. Each cluster is scored as a whole (≥ MIN_SECONDS of clean speech)
against enrolled profiles with two thresholds and a margin:
  score ≥ high and margin ≥ margin → «suggested»  (Probablemente X · confirmar)
  score ≥ low  and margin ≥ margin → «ask»        (¿Es X?)
  otherwise                        → «unknown»    (Hablante N)
Cluster centroids are kept encrypted per meeting so a profile change can be
re-evaluated without audio. Never train or enroll on meeting audio.
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

from .profiles import normalized
from .onboarding import samples, clean_samples, embed_chunk

THRESHOLDS = json.loads((Path(__file__).with_name("thresholds.json")).read_text())
MIN_SECONDS = THRESHOLDS["min_seconds"]
from .native import CROP_SAMPLES as CHUNK  # 10 s per embedding crop
from .matching_legacy import PILOT_THRESHOLD, PILOT_MARGIN, classify, configured_space, analysis_policy
SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{2,60}$")


def profile_signature(actor):
    from .sync import catalog
    profiles, signature, _ = catalog(actor)
    return signature, len(profiles)


def failure_cause(error: BaseException) -> str:
    """A stable, non-personal code for why the comparison failed."""
    text = str(error)
    if isinstance(error, ValueError) and SAFE_CODE.match(text):
        return text
    name = type(error).__name__
    if name == "TimeoutExpired":
        return "voice_keychain_timeout"
    if name == "InvalidTag":
        return "voice_key_mismatch"
    if name == "OperationalError":
        return "voice_store_busy"
    if isinstance(error, FileNotFoundError):
        return "voice_file_missing"
    if isinstance(error, PermissionError):
        return "voice_file_permission"
    if isinstance(error, OSError):
        return "voice_io_error"
    return "voice_unexpected_" + type(error).__name__.lower()[:40]


def mean_vector(vectors: list[list[float]]) -> list[float]:
    return normalized([sum(v[i] for v in vectors) / len(vectors) for i in range(256)])


def suggest(vector: list[float] | None, clean_seconds: float, profiles: list[dict],
            participants: set[str] | None = None, thresholds: dict | None = None) -> dict[str, Any]:
    """Classify one whole cluster. Never returns a name as an identity, only a candidate."""
    t = thresholds or THRESHOLDS
    unknown = {"state": "unknown", "similarity": None, "margin": None, "candidate_employee_id": None,
               "candidate_name": None, "profile_id": None, "clean_seconds": round(clean_seconds, 3)}
    if vector is None or clean_seconds < t["min_seconds"]:
        return {**unknown, "reason": "short_or_unclean"}
    pool = [p for p in profiles if not participants or p.get("employee_id") in participants]
    if not pool:
        return {**unknown, "reason": "no_active_profiles" if not profiles else "no_participant_profiles"}
    best: dict[str, tuple[float, dict]] = {}
    for profile in pool:
        score = sum(a * b for a, b in zip(vector, profile["vector"]))
        employee = profile.get("employee_id", profile["id"])
        if employee not in best or score > best[employee][0]:
            best[employee] = (score, profile)
    ranked = sorted(best.values(), key=lambda item: -item[0])
    score, profile = ranked[0]
    margin = score - (ranked[1][0] if len(ranked) > 1 else t["single_profile_floor"])
    base = {**unknown, "similarity": round(score, 4), "margin": round(margin, 4)}
    if margin < t["margin"] or score < t["low"]:
        return {**base, "reason": "ambiguous" if score >= t["low"] else "below_threshold"}
    state = "suggested" if score >= t["high"] else "ask"
    return {**base, "state": state, "reason": state, "profile_id": profile["id"],
            "candidate_employee_id": profile.get("employee_id"), "candidate_name": profile.get("name")}


def local_spans(words: list[dict], gap_ms: int = 400) -> list[dict]:
    """Speech spans of the local user from (echo-free) microphone word timings."""
    spans: list[dict] = []
    for word in sorted(words, key=lambda w: w["start_ms"]):
        if spans and word["start_ms"] - spans[-1]["end_ms"] <= gap_ms:
            spans[-1]["end_ms"] = max(spans[-1]["end_ms"], word["end_ms"])
        else:
            spans.append({"start_ms": word["start_ms"], "end_ms": word["end_ms"], "speaker_id": "local"})
    return spans


def cluster_prints(sources: dict[str, Path], diarizations: dict[str, dict], directory: Path, lock_fd=None,
                   embed=embed_chunk) -> dict[str, dict]:
    """Centroid of up to 3 crops of 5 s per cluster with ≥ MIN_SECONDS of non-overlapping speech."""
    prints = {}
    for source, path in sources.items():
        turns = diarizations.get(source, {}).get("segments") or []
        if not turns:
            continue
        audio = samples(path)
        for speaker in sorted({t["speaker_id"] for t in turns}):
            others = [t for t in turns if t["speaker_id"] != speaker]
            own = [t for t in turns if t["speaker_id"] == speaker and
                   not any(max(t["start_ms"], o["start_ms"]) < min(t["end_ms"], o["end_ms"]) for o in others)]
            clean = clean_samples(audio, own, speaker)
            seconds = len(clean) / 16000
            key = source + ":" + speaker
            if seconds < MIN_SECONDS:
                prints[key] = {"vector": None, "clean_seconds": seconds}
                continue
            # Up to 8 candidate crops spread over the cluster; keep the first 3 the model accepts
            # (it refuses a crop where it hears two voices, e.g. residual echo).
            candidates = min(8, max(1, len(clean) // (CHUNK // 2) - 1))
            offsets = [round(i * (len(clean) - CHUNK) / max(1, candidates - 1)) for i in range(candidates)]
            offsets = offsets[::3] + offsets[1::3] + offsets[2::3]
            vectors = []
            for i, offset in enumerate(offsets):
                if len(vectors) == 3:
                    break
                try:
                    vectors.append(embed(clean[offset:offset + CHUNK], directory, f"{source}-{speaker}-{i}", lock_fd))
                except ValueError as error:
                    # A crop the model refuses (e.g. two voices detected) is skipped, not fatal.
                    if str(error) not in ("speaker_inference_failed", "empty_embedding", "invalid_embedding"):
                        raise
            prints[key] = {"vector": mean_vector(vectors) if vectors else None, "clean_seconds": seconds,
                           "crops": len(vectors)}
    return prints


def classify_prints(prints: dict[str, dict], profiles: list[dict], participants=None, thresholds=None) -> dict[str, dict]:
    return {cluster: suggest(p["vector"], p["clean_seconds"], profiles, participants, thresholds)
            for cluster, p in sorted(prints.items())}


def compatible_profiles(actor, snapshot=None):
    from . import native
    from .sync import catalog
    profiles, signature, sync_state = snapshot if snapshot is not None else catalog(actor)
    model = native.embedding_model_id()
    return [p for p in profiles if p["model"] == model], signature, sync_state, model, len(profiles)


def _identify_crop10(actor, normalized_sources, diarizations, raw_root, lock_fd=None, task_id=None, local_words=None,
                     participants=None, snapshot=None):
    """Extract per-cluster prints (stored encrypted), then classify them against profiles."""
    from synth.worker.journal import file_hash
    profiles, signature, sync_state, model, total = compatible_profiles(actor, snapshot)
    result = {"state": "suggestions", "profile_signature": signature, "sync_state": sync_state, "model": model,
              "matching_mode": "two_threshold", "thresholds_id": THRESHOLDS["id"], "matches": {},
              "active_profiles": len(profiles)}
    raw_root = Path(raw_root).resolve(strict=True)
    paths = {}
    for source, desc in normalized_sources.items():
        path = Path(desc["path"]).resolve(strict=True)
        if not path.is_relative_to(raw_root) or file_hash(path) != desc["sha256"]:
            raise ValueError("identity_audio_changed")
        paths[source] = path
    diarizations = dict(diarizations)
    if local_words is not None and diarizations.get("microphone", {}).get("speech_state") == "local_user":
        diarizations["microphone"] = {"segments": local_spans(local_words)}
    with tempfile.TemporaryDirectory(prefix="identity-", dir=raw_root) as temporary:
        prints = cluster_prints(paths, diarizations, Path(temporary), lock_fd)
    if task_id:
        from .prints import open_prints
        store = open_prints()
        try:
            for cluster, value in prints.items():
                if value["vector"] is not None:
                    store.store(task_id, cluster, model, value["vector"], value["clean_seconds"])
        finally:
            store.close()
    if not total:
        return {**result, "state": "unavailable", "reason": "no_active_profiles", "prints_stored": bool(task_id)}
    if not profiles:
        return {**result, "state": "unavailable", "reason": "no_model_compatible_profiles"}
    result["matches"] = classify_prints(prints, profiles, participants)
    return result


def _reidentify_crop10(actor, task_id, participants=None, snapshot=None):
    """Re-evaluate a meeting after a profile change, from stored prints only (no audio)."""
    from .prints import open_prints
    profiles, signature, sync_state, model, total = compatible_profiles(actor, snapshot)
    store = open_prints()
    try:
        prints = store.load(task_id, model)
    finally:
        store.close()
    result = {"state": "suggestions", "profile_signature": signature, "sync_state": sync_state, "model": model,
              "matching_mode": "two_threshold", "thresholds_id": THRESHOLDS["id"], "matches": {},
              "active_profiles": len(profiles), "source": "stored_prints"}
    if not prints:
        return {**result, "state": "unavailable", "reason": "voiceprints_unavailable"}
    if not profiles:
        return {**result, "state": "unavailable", "reason": "no_active_profiles" if not total else "no_model_compatible_profiles"}
    result["matches"] = classify_prints(prints, profiles, participants)
    return result


def _policy_catalog(actor):
    from .native import LOCK, embedding_model_id
    from .sync import catalog
    snapshot = catalog(actor)
    bare_model = json.loads(LOCK.read_text())['model_revision']
    legacy = [p for p in snapshot[0] if p['model'] == bare_model]
    crop10 = [p for p in snapshot[0] if p['model'] == embedding_model_id()]
    return snapshot, bare_model, legacy, crop10


def _combine_policies(crop10, legacy):
    if legacy is None:
        return crop10
    legacy = dict(legacy)
    legacy['matches'] = {key: {**value, 'state': 'suggested' if value['state'] == 'candidate' else value['state'],
                               'candidate_name': value.get('label'), 'crop_policy': 'legacy_5s'}
                         for key, value in legacy.get('matches', {}).items()}
    if crop10 is None:
        return legacy
    result = {**crop10, 'matching_mode': 'policy_separated',
              'legacy_analysis_policy': legacy.get('analysis_policy'),
              'active_profiles': crop10.get('active_profiles', 0)+legacy.get('active_profiles', 0), 'matches': {}}
    for key in crop10.get('matches', {}).keys() | legacy['matches'].keys():
        candidates = [r for r in (crop10.get('matches', {}).get(key), legacy['matches'].get(key)) if r]
        proposals = [r for r in candidates if r['state'] in ('suggested', 'ask')]
        identities = {r.get('candidate_employee_id') or r.get('profile_id') for r in proposals}
        if len(identities) > 1:
            outcome = {'state': 'unknown', 'reason': 'crop_policy_disagreement', 'candidate_employee_id': None,
                       'candidate_name': None, 'profile_id': None, 'similarity': None}
        else:
            outcome = dict(next((r for r in proposals if r['state'] == 'suggested'), proposals[0] if proposals else candidates[0]))
        outcome['policy_evidence'] = candidates
        result['matches'][key] = outcome
    return result


def identify(actor, normalized_sources, diarizations, raw_root, lock_fd=None, task_id=None, local_words=None,
             participants=None):
    from .native import reuse_models
    with reuse_models(raw_root, lock_fd):
        return _identify_policies(actor, normalized_sources, diarizations, raw_root, lock_fd,
                                  task_id, local_words, participants)


def _identify_policies(actor, normalized_sources, diarizations, raw_root, lock_fd=None, task_id=None, local_words=None,
                       participants=None):
    """Compare each crop policy only with its own enrolled vectors and thresholds."""
    from . import matching_legacy
    from .learning import enabled as learning_enabled
    snapshot, model, legacy_profiles, crop10_profiles = _policy_catalog(actor)
    crop10 = None
    if crop10_profiles or not legacy_profiles or task_id and learning_enabled(actor):
        crop10 = _identify_crop10(actor, normalized_sources, diarizations, raw_root, lock_fd,
                                 task_id, local_words, participants, snapshot)
        if legacy_profiles and not crop10_profiles:
            # Keep compatible ten-second evidence for future shared profiles.
            # It supplements local storage, never converts legacy vectors.
            crop10 = None
    legacy = None
    if legacy_profiles:
        pool = [p for p in legacy_profiles if not participants or p.get('employee_id') in participants]
        ds = dict(diarizations)
        if local_words is not None and ds.get('microphone', {}).get('speech_state') == 'local_user':
            ds['microphone'] = {'segments': local_spans(local_words)}
        legacy = matching_legacy.identify(actor, normalized_sources, ds, raw_root, lock_fd,
            catalog_snapshot=(pool, snapshot[1], snapshot[2]), task_id=task_id)
    return _combine_policies(crop10, legacy)


def _reidentify_legacy(actor, task_id, snapshot, model, profiles, participants):
    from . import matching_legacy
    from .prints import open_prints
    from .native import project_embeddings
    space = configured_space()
    store = open_prints()
    try:
        prints = store.load_windows(task_id, model, analysis_policy(space))
    finally:
        store.close()
    pool = [p for p in profiles if not participants or p.get('employee_id') in participants]
    result = {'state': 'pilot_uncalibrated', 'matching_mode': 'pilot_uncalibrated',
              'profile_signature': snapshot[1], 'sync_state': snapshot[2], 'model': model,
              'active_profiles': len(pool), 'matches': {}, 'source': 'stored_prints',
              'analysis_policy': analysis_policy(space), 'embedding_space': space}
    if not prints:
        return {**result, 'state': 'unavailable', 'reason': 'voiceprints_unavailable'}
    with tempfile.TemporaryDirectory(prefix='voice-reidentify-') as directory:
        if pool and space == 'plda_rho128_v1':
            projected = project_embeddings([p['vector'] for p in pool], Path(directory))
            pool = [{**p, 'vector': v} for p, v in zip(pool, projected)]
        for cluster, evidence in prints.items():
            vectors = evidence['vectors']
            if vectors and space == 'plda_rho128_v1':
                vectors = project_embeddings(vectors, Path(directory))
            result['matches'][cluster] = matching_legacy.classify(vectors, pool, 128 if space == 'plda_rho128_v1' else 256)
    return result


def reidentify(actor, task_id, participants=None):
    snapshot, model, legacy_profiles, crop10_profiles = _policy_catalog(actor)
    crop10 = _reidentify_crop10(actor, task_id, participants, snapshot) if crop10_profiles or not legacy_profiles else None
    legacy = _reidentify_legacy(actor, task_id, snapshot, model, legacy_profiles, participants) if legacy_profiles else None
    return _combine_policies(crop10, legacy)


def analyze_existing(actor, task):
    from synth.worker.desktop_pipeline import STATE, CAPTURE, restore_json, save_json, inference_lock
    output = STATE / "tasks" / task["id"]
    raw = CAPTURE / ("processed-" + task["id"])
    cp = task["checkpoints"]
    with inference_lock(STATE) as fd:
        ds = restore_json(cp["diarization"], output)
        sources = restore_json(cp['acoustic'], output)['sources'] if 'acoustic' in cp else cp['normalized']
        result = identify(actor, sources, ds, raw, fd, task_id=task["id"])
        from .meeting_status import transcript_hash
        canonical = restore_json(cp["canonical"], output)
        result.update(processing_mode="automatic", transcript_hash=transcript_hash(canonical))
        save_json(output / "identity-status.json", result)
    import sqlite3
    with sqlite3.connect(STATE / "queue.sqlite3") as db:
        db.execute("UPDATE desktop_tasks SET result=json_set(result,'$.identity',json(?)) WHERE id=? AND state='succeeded'",
                   (json.dumps(result), task["id"]))
    return result
