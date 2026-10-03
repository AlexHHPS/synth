"""Local Core ML adapter; never returns profile vectors through the product API."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import wave

from synth.worker.journal import file_hash
from .profiles import normalized

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "synth/.runtime/swift-build/release/synth-speakers"
MODELS = ROOT / "synth/.runtime/models/speakers-community1"
LOCK = ROOT / "synth/resources/speaker-model-lock.json"
REVISION = "c388107348134698135cfd34f3f59dc823b6e7ce"
_VERIFIED = set()


def verify_models(root=MODELS):
    root = Path(root)
    manifest = json.loads(LOCK.read_text())["model_manifest"]
    for item in manifest["files"]:
        path = root / item["path"]
        if not path.is_file():
            raise ValueError("speaker_model_missing")
        stat = path.stat()
        signature = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, item["sha256"])
        if signature not in _VERIFIED:
            if stat.st_size != item["size"] or file_hash(path) != item["sha256"]:
                raise ValueError("speaker_model_hash_mismatch")
            _VERIFIED.add(signature)


def infer(operation, audio, working_directory, binary=BINARY, models=MODELS, lock_fd=None):
    if operation not in {"diarize", "embed"}:
        raise ValueError("speaker_operation_invalid")
    audio, binary, models = Path(audio), Path(binary), Path(models)
    if not binary.is_file():
        raise ValueError("speaker_binary_missing")
    verify_models(models)
    with wave.open(str(audio)) as stream:
        if stream.getnchannels() != 1 or stream.getframerate() != 16000 or stream.getsampwidth() != 2:
            raise ValueError("speaker_input_must_be_mono_16khz_pcm16")
        duration_ms = round(stream.getnframes() / 16)
    directory = Path(working_directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    started = time.monotonic()
    # Embeddings exist briefly in a private temporary directory and are then erased.
    # This directory is inside the caller's managed raw/temporary retention tree.
    with tempfile.TemporaryDirectory(prefix="speaker-", dir=directory) as temporary:
        output = Path(temporary) / "result.json"
        try:
            process = subprocess.run([str(binary), operation, str(audio), str(models), str(output)],
                                     capture_output=True, timeout=min(4000, duration_ms / 1000 * 3 + 120),
                                     pass_fds=() if lock_fd is None else (lock_fd,))
        except subprocess.TimeoutExpired:
            raise ValueError("speaker_inference_timeout") from None
        if process.returncode or not output.is_file():
            raise ValueError("speaker_inference_failed")
        result = json.loads(output.read_text())
    if result.get("mode") != "real" or result.get("revision") != REVISION:
        raise ValueError("speaker_runtime_mismatch")
    if operation == "embed":
        result["embedding"] = normalized(result.get("embedding", []))
    else:
        segments = result.get("segments")
        if not isinstance(segments, list):
            raise ValueError("speaker_output_invalid")
        if result.get("speech_state", "detected") not in {"detected", "no_usable_speech"}:
            raise ValueError("speaker_output_invalid")
        if result.get("speech_state") == "no_usable_speech" and segments:
            raise ValueError("speaker_output_invalid")
        for segment in segments:
            start, end = segment.get("start_ms"), segment.get("end_ms")
            if (not isinstance(start, int) or not isinstance(end, int) or
                    not 0 <= start < end <= duration_ms + 100 or
                    not isinstance(segment.get("speaker_id"), str) or not segment["speaker_id"]):
                raise ValueError("speaker_segment_invalid")
            segment["end_ms"] = min(end, duration_ms)
        result["segments"] = sorted(segments, key=lambda s: (s["start_ms"], s["end_ms"], s["speaker_id"]))
    result.update({"audio_sha256": file_hash(audio), "duration_ms": duration_ms,
                   "elapsed_seconds": time.monotonic() - started,
                   "model_revision": json.loads(LOCK.read_text())["model_revision"]})
    return result


def attach_speaker_labels(transcript, diarization):
    """Keep ASR words intact; abstain where a segment includes competing speakers."""
    result = json.loads(json.dumps(transcript))
    for segment in result["segments"]:
        segment["speaker_id"] = None
        segment["employee_id"] = None
        evidence = {}
        for turn in diarization["segments"]:
            overlap = max(0, min(segment["end_ms"], turn["end_ms"]) - max(segment["start_ms"], turn["start_ms"]))
            if overlap:
                evidence[turn["speaker_id"]] = evidence.get(turn["speaker_id"], 0) + overlap
        duration = segment["end_ms"] - segment["start_ms"]
        if len(evidence) == 1 and duration > 0:
            speaker, overlap = next(iter(evidence.items()))
            if overlap / duration >= 0.5:
                segment["speaker_id"] = speaker
    return result
