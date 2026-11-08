"""Host acoustic worker. Docker owns text/documents; this process owns audio.

One OS lock covers the whole acoustic worker lifetime. Queue leases fence
checkpoints; idempotent API receipts fence remote writes after host crashes.
No employee identity is asserted before independent calibration is available.
"""
from contextlib import contextmanager
import fcntl
import json
import os
import ssl
import shutil
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
from uuid import uuid4

from synth.contracts.transcript import Transcript
from synth.speakers.native import infer, attach_speaker_labels
from .asr import normalize_audio, transcribe
from .desktop_queue import DesktopQueue, QueueError
from .journal import file_hash
from .acoustic import prepare_acoustic_sources

ROOT = Path(__file__).resolve().parents[2]
APPDATA = Path.home() / "Library/Application Support/dev.synth.voice"
DOCUMENT_WAIT_S = 600  # how long the host waits for the cloud minutes before reporting them pending
CAPTURE = APPDATA / "capture/raw"
STATE = APPDATA / "pipeline"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class LocalAPI:
    def __init__(self, credential=APPDATA / "desktop-api-key"):
        self.credential = Path(credential)
        from synth.config import API_URL, AUTH_MODE
        configuration = APPDATA / "backend-config.json"
        self.base_url = json.loads(configuration.read_text()).get("api_url") if configuration.exists() else API_URL
        from synth.config import API_URL
        if self.base_url != API_URL:
            raise ValueError("unapproved_product_backend")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile="/etc/ssl/cert.pem")))
        self.auth_mode = json.loads(configuration.read_text()).get("auth_mode", "keys") if configuration.exists() else AUTH_MODE
        self.expected_principal = None

    def assert_current_principal(self):
        if self.auth_mode == 'supabase' and self.expected_principal is not None:
            from synth.server.desktop_auth import access
            if access()['principal_id'] != self.expected_principal:
                raise ValueError('auth_identity_changed')

    def call(self, method, path, body=None):
        if self.auth_mode == "supabase":
            from synth.server.desktop_auth import access
            session = access()
            if self.expected_principal is not None and self.expected_principal != session["principal_id"]:
                raise ValueError("auth_identity_changed")
            self.expected_principal = session["principal_id"]
            token = session["access_token"]
        else:
            token = self.credential.read_text().strip()
        request = urllib.request.Request(self.base_url + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=20) as response:
                content = response.read(16 * 1024 * 1024 + 1)
                if len(content) > 16 * 1024 * 1024:
                    raise ValueError("api_response_too_large")
                return json.loads(content)
        except urllib.error.HTTPError as error:
            raise ValueError("api_http_" + str(error.code)) from None
        except (urllib.error.URLError, TimeoutError):
            raise ValueError("api_unavailable") from None


def save_json(path, content):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".part")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(content, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {"path": str(path), "sha256": file_hash(path)}


def restore_json(descriptor, root):
    path = Path(descriptor["path"]).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or file_hash(path) != descriptor["sha256"]:
        raise ValueError("checkpoint_file_invalid")
    return json.loads(path.read_text())


def delete_processed_audio(task, capture):
    """Erase only this closed capture and its normalized copies, before LLM work."""
    root = Path(capture).resolve(strict=True)
    paths = [Path(d["path"]) for d in task["metadata"]["sources"].values()]
    normalized = root / ("processed-" + task["id"])
    if normalized.exists() and not normalized.is_symlink():
        paths.extend(p for p in normalized.rglob("*") if p.is_file())
    for descriptor in task["metadata"]["sources"].values():
        source = Path(descriptor["path"])
        receipt = source.parent / "capture.json"
        if source.parent.name == "sources" and receipt.is_file() and not receipt.is_symlink():
            data = json.loads(receipt.read_text())
            if data.get("state") == "closed" and data.get("capture_id") == task["metadata"].get("capture_id"):
                paths.extend(p for p in source.parent.parent.rglob("*") if p.is_file())
    removed = 0
    for path in set(paths):
        if path.suffix.lower() not in {".wav", ".mp4", ".m4a", ".flac", ".mp3", ".ogg", ".opus", ".webm", ".aac", ".aiff", ".aif"}:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("audio_cleanup_path_denied")
        if path.exists():
            path.unlink()
            removed += 1
    return removed


def release_audio(task, capture, output, normalized, eval_store=None, eval_key=None):
    """Erase audio once acoustic processing ends; an eval-marked meeting first keeps
    an encrypted host-only copy. A cloud retry never requires raw audio."""
    import logging
    from synth.eval import retention
    options = {k: v for k, v in (("store", eval_store), ("key", eval_key)) if v is not None}
    receipt = {"retained": False}
    try:
        receipt = retention.retain(task, normalized, **options)
    except Exception as error:  # the opt-in copy must never keep audio on disk (default policy first)
        receipt = {"retained": False, "error": type(error).__name__}
        logging.getLogger("synth.worker").warning("eval_retention_failed cause=%s", type(error).__name__)
    finally:
        removed = delete_processed_audio(task, capture)
    save_json(Path(output) / "audio-deletion.json", {"deleted": True, "after": "acoustic_processing",
              "at": time.time(), "eval_retained": receipt["retained"]})
    return {"removed": removed, "eval_retained": receipt["retained"]}


@contextmanager
def inference_lock(state):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(state / "acoustic-worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("acoustic_worker_already_running") from None
        yield fd
    finally:
        os.close(fd)


class Lease:
    def __init__(self, database, capture, task):
        self.database, self.capture, self.task = database, capture, task
        self.stop = threading.Event()
        self.failure = None

    def __enter__(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def run(self):
        queue = None
        try:
            queue = DesktopQueue(self.database, self.capture)
            while not self.stop.wait(5):
                if not queue.heartbeat(self.task["id"], self.task["lease_token"], lease_seconds=60):
                    self.failure = "task_cancel_requested"
                    return
        except Exception:
            self.failure = "lease_heartbeat_failed"
        finally:
            if queue is not None:
                queue.close()

    def check(self, queue):
        if self.failure:
            raise QueueError(self.failure)
        if not queue.heartbeat(self.task["id"], self.task["lease_token"], lease_seconds=60):
            raise QueueError("task_cancel_requested")

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=15)


def is_remote(transcripts, diarizations):
    """A meeting is remote when the system channel carries speech."""
    system = diarizations.get("system")
    return "system" in transcripts and bool(system) and system.get("speech_state") != "no_usable_speech" \
        and bool(system.get("segments"))


def independent_capture(task):
    """The owner-bound source archive distinguishes new captures from legacy ones.

    A completed live ASR receipt is optional: a failed live window must not
    change the channel/identity policy used when recovering the same audio.
    """
    capture_id = task['metadata'].get('capture_id')
    if not capture_id:
        return False
    parents = {Path(d['path']).parent for d in task['metadata']['sources'].values()}
    if len(parents) != 1:
        return False
    parent = parents.pop()
    if parent.name != 'sources' or not (parent.parent/'capture-owner.json').exists():
        return False
    from .live_voice import load_owner
    owner = load_owner(parent.parent, capture_id)
    expected = task['metadata'].get('owner_id')
    if expected is not None and not owner.startswith('keys:') and owner != expected:
        raise ValueError('capture_owner_changed')
    return True


def echo_segment_ids(transcript, removed_words):
    """Microphone segments whose words are (almost) all echo of the system channel."""
    removed = {(w["start_ms"], w["end_ms"], w["text"]) for w in removed_words}
    by_segment = {}
    for word in transcript.get("words", []):
        key = word.get("segment_index")
        hit = (word["start_ms"], word["end_ms"], word["text"]) in removed
        total, echoed = by_segment.get(key, (0, 0))
        by_segment[key] = (total + 1, echoed + hit)
    return {f"s{index:06d}" for index, (total, echoed) in by_segment.items() if total and echoed / total >= 0.8}


def canonical_transcript(transcripts, diarizations, words=None, asr_evidence=None, *, independent_channels=False):
    """Preserve source provenance; channel identity is never employee identity.

    `words` (per source, from ASR word timings) adds readable turns. In a remote
    meeting the microphone is the local user and its echo of the system channel
    is removed; an in-person microphone keeps its diarized speakers.
    """
    from .bleed import drop_echo_words
    from .transcript_quality import is_hallucination
    from .turns import LOCAL_SPEAKER, build_turns, dominant_speaker, label_words
    segments = []
    duration = 0
    fingerprints = []
    has_voice = any(d.get("segments") for d in diarizations.values())
    remote = is_remote(transcripts, diarizations)
    words = dict(words or {})
    independent_channels = independent_channels or asr_evidence is not None
    echo_ids = set()
    filtered_microphone = None
    # New AEC-prepared captures keep separate channels. Text agreement alone is
    # not acoustic proof of echo: two people can say the same words together.
    if not independent_channels and remote and words.get("microphone") and words.get("system"):
        kept, _ = drop_echo_words(words["microphone"], words["system"])
        kept_keys = {(w["start_ms"], w["end_ms"], w["text"]) for w in kept}
        removed = [w for w in words["microphone"] if (w["start_ms"], w["end_ms"], w["text"]) not in kept_keys]
        echo_ids = echo_segment_ids({"words": words["microphone"]}, removed)
        words["microphone"] = kept
        if removed:
            # Keep phrase text consistent with the words handed to diarization.
            # The original hypothesis remains in ASR checkpoints/evidence.
            microphone = transcripts['microphone']
            by_phrase = {}
            for word in kept:
                index = word.get('segment_index', 0 if len(microphone['segments']) == 1 else None)
                if type(index) is not int or not 0 <= index < len(microphone['segments']):
                    raise ValueError('echo_word_provenance_missing')
                by_phrase.setdefault(index, []).append(word)
            filtered_microphone = {**microphone, 'segments': [
                {**segment, 'text': ' '.join(w['text'] for w in by_phrase[index]),
                 'start_ms': by_phrase[index][0]['start_ms'], 'end_ms': max(w['end_ms'] for w in by_phrase[index])}
                for index, segment in enumerate(microphone['segments']) if index in by_phrase]}
            filtered_microphone['words'] = kept
    labelled_words = []
    for source, transcript in transcripts.items():
        duration = max(duration, transcript["duration_ms"])
        fingerprints.append(transcript["model_fingerprint"])
        # Once another source contains usable speech, an empty acoustic source
        # contributes no speakers or ASR hallucinations to the meeting. Raw ASR
        # remains in its checkpoint. Very short notes may produce no embedding
        # on any source: retain their ASR with unknown labels rather than dropping it.
        if has_voice and diarizations[source].get("speech_state") == "no_usable_speech" and not independent_channels:
            continue
        # The local user: a remote meeting's microphone, or an in-person microphone with one dominant voice.
        local = not independent_channels and source == "microphone" and (remote or dominant_speaker(diarizations[source]) is not None)
        source_transcript = filtered_microphone if source == 'microphone' and filtered_microphone is not None else transcript
        excluded_echo = echo_ids if filtered_microphone is None else set()
        labelled = attach_speaker_labels(source_transcript, diarizations[source], words=words.get(source) or source_transcript.get('words'))
        hallucinated = {segment["id"] for segment in labelled["segments"] if is_hallucination(segment["text"])}
        for segment in labelled["segments"]:
            if (source == "microphone" and segment["id"] in excluded_echo) or segment["id"] in hallucinated:
                continue
            segment["source_id"] = source
            if local:
                segment["speaker_id"] = source + ":" + LOCAL_SPEAKER
            elif segment["speaker_id"]:
                segment["speaker_id"] = source + ":" + segment["speaker_id"]
            segment["origin_id"] = segment["id"]
            segments.append(segment)
        if words.get(source):
            # Diarization can split a phrase into several segments. Resolve word
            # citations against those actual segments rather than the old ASR
            # phrase index, which no longer names the same source span.
            spoken, cursor = [], 0
            for segment in labelled['segments']:
                tokens = []
                while len(tokens) < len(segment['text'].split()) and cursor < len(words[source]):
                    word = words[source][cursor]
                    tokens.extend(word['text'].split()); cursor += 1
                    if segment['id'] not in hallucinated and not (source == 'microphone' and segment['id'] in excluded_echo):
                        spoken.append({**word, 'source_id': source, 'origin': segment['id'],
                                       **({'speaker_id': segment['speaker_id']} if independent_channels else {})})
                if tokens != segment['text'].split():
                    raise ValueError('speaker_word_alignment_mismatch')
            # Turns and segments share one rule for the local user, so a turn never names another speaker than its segments.
            tagged = spoken if independent_channels else (
                [{**w, "speaker_id": source + ":" + LOCAL_SPEAKER} for w in spoken] if local
                else label_words(spoken, diarizations[source], source))
            labelled_words += tagged
    segments.sort(key=lambda segment: (segment["start_ms"], segment["source_id"], segment["end_ms"]))
    renamed = {}
    for index, segment in enumerate(segments):
        renamed[(segment["source_id"], segment.pop("origin_id"))] = f"s{index:06d}"
        segment["id"] = f"s{index:06d}"
    if not segments:
        raise ValueError("no_speech_detected")
    result = {"language": "es", "duration_ms": duration, "model_fingerprint": fingerprints[0], "segments": segments}
    if asr_evidence is not None:
        result['asr_evidence'] = asr_evidence
    if labelled_words:
        turns = build_turns(labelled_words)
        for turn in turns:
            members = turn.pop("members")
            turn["segment_ids"] = sorted({renamed[(w["source_id"], w["origin"])] for w in members
                                          if (w["source_id"], w["origin"]) in renamed})
            turn["employee_id"] = None
        result["turns"] = turns
    return Transcript.model_validate(result).model_dump()


def meeting_prompt(api, folder_id=None):
    """Approved glossary terms (folder first, then workspace) as the whisper prompt; None if unavailable."""
    import logging
    from .asr import glossary_prompt
    try:
        path = "/v1/glossary?status=aprobado" + (f"&folder_id={folder_id}" if folder_id else "")
        items = api.call("GET", path).get("items", [])
    except (ValueError, OSError, AttributeError) as error:
        logging.getLogger("synth.worker").info("glossary_prompt_unavailable cause=%s", type(error).__name__)
        return None
    items = sorted(items, key=lambda t: (t.get("scope") != "folder", -int(t.get("occurrences") or 0)))
    return glossary_prompt([t["term"] for t in items])


def gate_bleed(normalized, raw):
    """Mute microphone frames that only carry the speakers' echo (remote meetings)."""
    if not {"microphone", "system"} <= set(normalized):
        return {"skipped": True}
    import wave
    from .bleed import gate_file
    target = Path(raw) / "microphone.gated.wav"
    try:
        stats = gate_file(Path(normalized["microphone"]["path"]), Path(normalized["system"]["path"]), target)
    except (wave.Error, ValueError, EOFError) as error:
        # The text-level echo removal still runs; a gate failure never blocks the meeting.
        return {"skipped": True, "reason": "gate_unavailable:" + type(error).__name__}
    target.chmod(0o600)
    return {"path": str(target), "sha256": file_hash(target), "stats": stats}


def asr_sources(normalized, bleed, raw):
    sources = {source: descriptor["path"] for source, descriptor in normalized.items()}
    if bleed.get("path"):
        path = Path(bleed["path"]).resolve(strict=True)
        if not path.is_relative_to(Path(raw).resolve()) or file_hash(path) != bleed["sha256"]:
            raise ValueError("gated_audio_invalid")
        sources["microphone"] = str(path)
    return sources


def diarize_sources(normalized, raw, check, lock_fd=None, independent_channels=False):
    """New captures analyze both sources; keep the old role policy for legacy jobs."""
    diarizations = {}
    for source in sorted(normalized, key=lambda name: name != "system"):
        check()
        if not independent_channels and source == "microphone" and is_remote(normalized, diarizations):
            diarizations[source] = {"speech_state": "local_user", "segments": []}
            continue
        diarizations[source] = infer("diarize", normalized[source]["path"], raw, lock_fd=lock_fd)
    return diarizations


def meeting_identity(actor, task, checkpoints, output, raw, lock_fd=None):
    """Voice suggestions for a meeting; the cause of a failure is always recorded."""
    import logging
    from synth.speakers.matching import failure_cause, identify, profile_signature, reidentify
    from synth.speakers.meeting_status import transcript_hash
    identity_path = output / "identity-status.json"
    canonical_hash = transcript_hash(restore_json(checkpoints["canonical"], output))
    # Participant prior: when the capture names its participants, only they are candidates.
    participants = set(task.get("metadata", {}).get("participants") or []) or None
    try:
        if identity_path.exists():
            identity = json.loads(identity_path.read_text())
            signature, _ = profile_signature(actor)
            if identity.get("transcript_hash") not in (None, canonical_hash):
                return {"state": "unavailable", "reason": "transcript_version_changed", "matches": {}}
            if identity.get("profile_signature") == signature:
                return identity
            # A changed profile is re-evaluated from the stored voiceprints; no audio needed.
            return reidentify(actor, task["id"], participants=participants)
        diarizations = restore_json(checkpoints["diarization"], output)
        local_words = None
        if diarizations.get("microphone", {}).get("speech_state") == "local_user" and "asr" in checkpoints:
            transcripts = restore_json(checkpoints["asr"], output)
            words = {source: t.get("words", []) for source, t in transcripts.items()}
            from .bleed import drop_echo_words
            local_words, _ = drop_echo_words(words.get("microphone", []), words.get("system", []))
        sources = restore_json(checkpoints["acoustic"], output)["sources"] if "acoustic" in checkpoints else dict(checkpoints["normalized"])
        bleed = checkpoints.get("bleed") or {}
        if "acoustic" not in checkpoints and bleed.get("path"):
            sources["microphone"] = {"path": bleed["path"], "sha256": bleed["sha256"]}
        return identify(actor, sources, diarizations, raw, lock_fd, task_id=task["id"], local_words=local_words,
                        participants=participants)
    except Exception as error:  # a voice failure never fails the meeting; the cause is always recorded
        cause = failure_cause(error)
        logging.getLogger("synth.worker").warning("voice_comparison_failed cause=%s", cause)
        return {"state": "unavailable", "reason": "voice_comparison_failed", "cause": cause, "matches": {}}


def consolidated_input(task, capture_root):
    """Only a closed capture's hash-bound receipt can replace final ASR work."""
    from .consolidated_capture import load
    sources = task['metadata']['sources']
    parents = {Path(d['path']).parent for d in sources.values()}
    if not task['metadata'].get('capture_id') or len(parents) != 1:
        return None
    parent = parents.pop()
    if parent.name != 'sources':
        return None
    folder = parent.parent.resolve(strict=True)
    if not folder.is_relative_to(capture_root.resolve(strict=True)):
        raise ValueError('consolidation_capture_path_invalid')
    return load(folder, task['metadata']['capture_id'], sources)


def copy_consolidated_sources(receipt, task, raw):
    """Use one prepared source set for diarization, comparison and retention.

    The capture copy is immutable; interrupted adoption can copy it again without
    touching Qwen. Pipeline paths stay inside its existing processed directory.
    """
    folder = Path(next(iter(task['metadata']['sources'].values()))['path']).parent.parent.resolve(strict=True)
    sources = {}
    for source, descriptor in receipt['prepared_sources'].items():
        original = folder/descriptor['path']
        if original != original.resolve(strict=True) or not original.is_relative_to(folder) or file_hash(original) != descriptor['sha256']:
            raise ValueError('consolidation_prepared_changed')
        target = raw/(source+'.live-prepared.wav')
        temporary = raw/(source+'.copy-'+str(uuid4())+'.wav')
        try:
            with os.fdopen(os.open(temporary, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600), 'wb') as destination:
                with original.open('rb') as incoming:
                    shutil.copyfileobj(incoming, destination)
                destination.flush(); os.fsync(destination.fileno())
            if file_hash(temporary) != descriptor['sha256']:
                raise ValueError('consolidation_prepared_changed')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        sources[source] = {'path': str(target), 'sha256': descriptor['sha256']}
    directory = os.open(raw, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return sources


def process(queue, task, lease, api, state, lock_fd=None):
    policy = json.loads((ROOT / "synth/resources/runtime-policy.json").read_text())
    if policy.get("id") != "operator-configured-gateway-v1":
        raise ValueError("runtime_policy_missing")
    actor = api.call("GET", "/v1/me")
    if task["metadata"].get("owner_id") is not None and task["metadata"]["owner_id"] != actor["id"]:
        raise ValueError("capture_owner_changed")
    if getattr(api,"auth_mode",None)=="supabase" and task["metadata"].get("owner_id") is None:
        raise ValueError("capture_owner_missing")
    if actor["kind"] != "user" or actor["id"] == "00000000-0000-4000-8000-000000000001":
        raise ValueError("desktop_identity_must_be_nonadmin_user")
    identifier, token = task["id"], task["lease_token"]
    checkpoints = task["checkpoints"]
    output = Path(state) / "tasks" / identifier
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = queue.capture_root / ("processed-" + identifier)
    raw.mkdir(parents=True, exist_ok=True, mode=0o700)

    def checkpoint(stage, descriptor):
        lease.check(queue)
        queue.checkpoint(identifier, token, stage, descriptor)
        checkpoints[stage] = descriptor

    if "canonical" in checkpoints:
        canonical = restore_json(checkpoints["canonical"], output)
    else:
        live = None
        if 'consolidated' in checkpoints:
            live = restore_json(checkpoints['consolidated'], output)
        elif not any(stage in checkpoints for stage in ('normalized', 'acoustic', 'asr', 'diarization', 'asr_microphone', 'asr_system')):
            lease.check(queue)
            live = consolidated_input(task, queue.capture_root)
            if live is not None:
                checkpoint('consolidated', save_json(output/'consolidated.json', live))
        if live is not None and 'asr' not in checkpoints:
            lease.check(queue)
            prepared_sources = copy_consolidated_sources(live, task, raw)
            checkpoint('normalized', prepared_sources)
            checkpoint('acoustic', save_json(output/'acoustic.json', {
                'sources': prepared_sources, 'echo': {'engine': 'aec3', 'already_applied_in_live_capture': True},
                'capture_id': live['capture_id']}))
            checkpoint('asr', save_json(output/'asr.json', live['transcripts']))
        normalized = checkpoints.get("normalized")
        if normalized is None:
            normalized = {}
            for source, descriptor in task["metadata"]["sources"].items():
                lease.check(queue)
                source_path = Path(descriptor["path"]).resolve(strict=True)
                if not source_path.is_relative_to(queue.capture_root) or file_hash(source_path) != descriptor["sha256"]:
                    raise ValueError("capture_file_changed")
                target = raw / (source + ".wav")
                normalize_audio(source_path, target, lock_fd=lock_fd)
                target.chmod(0o600)
                normalized[source] = {"path": str(target), "sha256": file_hash(target)}
            checkpoint("normalized", normalized)
        for descriptor in normalized.values():
            path = Path(descriptor["path"]).resolve(strict=True)
            if not path.is_relative_to(raw) or file_hash(path) != descriptor["sha256"]:
                raise ValueError("normalized_audio_invalid")
        # Resume old ASR/diarization checkpoints against their original inputs.
        # New work uses one prepared source set for all acoustic operations.
        if 'acoustic' not in checkpoints and 'asr' not in checkpoints and 'diarization' not in checkpoints:
            lease.check(queue)
            prepared = prepare_acoustic_sources(normalized, raw, lock_fd=lock_fd)
            checkpoint('acoustic', save_json(output/'acoustic.json', prepared))
        acoustic = restore_json(checkpoints['acoustic'], output)['sources'] if 'acoustic' in checkpoints else normalized
        for descriptor in acoustic.values():
            path = Path(descriptor['path']).resolve(strict=True)
            if not path.is_relative_to(raw) or file_hash(path) != descriptor['sha256']:
                raise ValueError('acoustic_audio_invalid')
        if "asr" in checkpoints:
            transcripts = restore_json(checkpoints["asr"], output)
        else:
            # Only a pending ASR needs the gated audio; a resumed task with ASR done does not.
            prompt = meeting_prompt(api, task["metadata"].get("folder_id"))
            transcripts = {}
            for source, descriptor in acoustic.items():
                lease.check(queue)
                source_stage = 'asr_' + source
                if source_stage in checkpoints:
                    saved = restore_json(checkpoints[source_stage], output)
                    if saved['audio_sha256'] != descriptor['sha256']:
                        raise ValueError('asr_source_changed')
                    transcripts[source] = saved['transcript']
                else:
                    transcript, raw_asr = transcribe(descriptor["path"], output / source, source_id=source, lock_fd=lock_fd, prompt=prompt)
                    if raw_asr.get('words'):
                        transcript['words'] = raw_asr['words']
                    checkpoint(source_stage, save_json(output / (source_stage + '.json'),
                               {'audio_sha256': descriptor['sha256'], 'transcript': transcript}))
                    transcripts[source] = transcript
            checkpoint("asr", save_json(output / "asr.json", transcripts))
        if "diarization" in checkpoints:
            diarizations = restore_json(checkpoints["diarization"], output)
        else:
            diarizations = diarize_sources(acoustic, raw, lambda: lease.check(queue), lock_fd,
                                           independent_channels=independent_capture(task))
            checkpoint("diarization", save_json(output / "diarization.json", diarizations))
        words = {source: t.get("words", []) for source, t in transcripts.items()}
        plain = {source: {k: v for k, v in t.items() if k != "words"} for source, t in transcripts.items()}
        asr_evidence = live['asr_evidence'] if live is not None else None
        canonical = canonical_transcript(plain, diarizations, words, asr_evidence,
                                         independent_channels=independent_capture(task))
        from .transcript_quality import batch_quality_report, select_canonical
        try:
            canonical, quality = select_canonical(plain, canonical, task, queue.capture_root, diarizations, words,
                                                allow_live_fallback=live is None and not independent_capture(task))
            if live is not None:
                quality.update(selected='consolidated_live', repeated_asr_calls=0)
        except ValueError as error:
            quality = batch_quality_report(plain, canonical)
            quality.update(selected='blocked', reason=str(error))
            save_json(output / 'transcript-quality.json', quality)
            raise
        save_json(output / 'transcript-quality.json', quality)
        from synth.speakers.finalize_capture import export_confirmations
        canonical, voice_actions = export_confirmations(canonical, task, actor, api, Path(state), queue.capture_root, APPDATA)
        if voice_actions:
            checkpoint('voice_plan', save_json(output/'voice-plan.json', {'actions': voice_actions}))
        checkpoint("canonical", save_json(output / "transcript.json", canonical))
    # Suggestions only: no candidate name, score or vector enters the transcript or
    # the document provider. Names appear after the owner confirms them.
    identity = meeting_identity(actor, task, checkpoints, output, raw, lock_fd)
    from synth.speakers.meeting_status import transcript_hash
    canonical_hash = transcript_hash(canonical)
    identity.update(processing_mode='automatic', transcript_hash=canonical_hash)
    save_json(output / "identity-status.json", identity)
    if 'identified' not in checkpoints:
        checkpoint('identified', {'mode': 'automatic', 'transcript_hash': canonical_hash})
    release_audio(task, queue.capture_root, output, checkpoints["normalized"])
    lease.check(queue)
    if "uploaded" not in checkpoints:
        meeting_body = {"title": task["metadata"]["title"],
            "folder_id": task["metadata"]["folder_id"], "idempotency_key": "desktop:" + identifier}
        if task['metadata'].get('calendar_event_id'):
            meeting_body['calendar_event_id'] = task['metadata']['calendar_event_id']
        meeting = api.call("POST", "/v1/meetings", meeting_body)
        if task["metadata"].get("notes") and meeting["revision"] == 1:
            lease.check(queue)
            api.call("PUT", f'/v1/meetings/{meeting["id"]}/notes',
                {"notes": task["metadata"]["notes"], "expected_revision": meeting["revision"]})
        lease.check(queue)
        job = api.call("POST", f'/v1/meetings/{meeting["id"]}/transcript',
            {"transcript": canonical, "idempotency_key": "desktop:" + identifier + ":canonical"})
        checkpoint("uploaded", {"meeting_id": meeting["id"], "job_id": job["job_id"],
                                "transcript_version": job.get('transcript_version')})
    upload = checkpoints["uploaded"]
    if 'voice_plan' in checkpoints:
        if 'voice_applied' not in checkpoints:
            actions = restore_json(checkpoints['voice_plan'], output)['actions']
            lease.check(queue)
            applied = api.call('POST', f'/v1/meetings/{upload["meeting_id"]}/speakers/batch',
                {'actions': [{**action, 'expected_version': upload['transcript_version']} for action in actions]})
            checkpoint('voice_applied', {**upload, 'job_id': applied['job_id'],
                                       'transcript_version': applied['transcript_version']})
        upload = checkpoints['voice_applied']
    # The cloud owns document processing. Release the acoustic lane as soon as
    # its upload receipt is durable; task_status projects the actual document
    # state for clients, including failures, cancellation and new versions.
    return {**upload, "identity": identity, "runtime_policy_id": policy["id"]}


def work_once(database=STATE / "queue.sqlite3", capture=CAPTURE, state=STATE, api=None):
    with inference_lock(state) as lock_fd:
        queue = DesktopQueue(database, capture)
        try:
            task = queue.claim()
            if task is None:
                return None
            with Lease(database, capture, task) as lease:
                try:
                    result = process(queue, task, lease, api or LocalAPI(), state, lock_fd=lock_fd)
                    lease.check(queue)
                    return queue.finish(task["id"], task["lease_token"], result=result)
                except (ValueError, OSError) as error:
                    current = queue.get(task["id"])
                    uploaded = current["checkpoints"].get("uploaded")
                    if current["state"] == "cancel_requested" and uploaded:
                        try:
                            (api or LocalAPI()).call("POST", "/v1/jobs/" + uploaded["job_id"] + "/cancel")
                        except (ValueError, OSError):
                            # Local cancellation still takes effect. The text job
                            # stays inspectable separately if its API is unavailable.
                            pass
                    code = str(error)
                    if not code.replace("_", "").isalnum() or len(code) > 100:
                        code = "host_pipeline_failed"
                    return queue.finish(task["id"], task["lease_token"], error_code=code)
                except Exception as error:
                    # Unexpected failures must not strand a leased task and repeat
                    # it forever. Log locations/classes only, never meeting text.
                    import logging
                    import traceback
                    frames = ','.join(f'{Path(f.filename).name}:{f.lineno}:{f.name}'
                                      for f in traceback.extract_tb(error.__traceback__))
                    logging.getLogger('synth.worker').error('host_pipeline_failed cause=%s frames=%s',
                                                            type(error).__name__, frames)
                    return queue.finish(task['id'], task['lease_token'], error_code='host_pipeline_failed')
        finally:
            queue.close()
