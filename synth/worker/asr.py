"""Native Whisper adapter. No reference text or domain corrections are accepted."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import wave

from synth.contracts.transcript import Transcript
from .journal import file_hash

MODEL = Path(os.environ["SYNTH_ASR_MODEL"]) if os.environ.get("SYNTH_ASR_MODEL") else Path(__file__).resolve().parents[2] / "synth/.runtime/models/ggml-large-v3-turbo-q5_0.bin"
MODEL_SHA = "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"
_VERIFIED = set()


def transcribe(wav, working_directory, source_id="import", model=MODEL, lock_fd=None):
    wav, directory, model = Path(wav), Path(working_directory), Path(model)
    if not model.is_file():
        raise ValueError("asr_model_missing")
    stat = model.stat()
    signature = (str(model.resolve()), stat.st_size, stat.st_mtime_ns)
    if signature not in _VERIFIED:
        if file_hash(model) != MODEL_SHA:
            raise ValueError("asr_model_hash_mismatch")
        _VERIFIED.add(signature)
    with wave.open(str(wav)) as audio:
        if audio.getframerate() != 16_000 or audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise ValueError("asr_input_must_be_mono_16khz_pcm16")
        duration_ms = round(audio.getnframes() / 16)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    native_path = directory / "whisper-native"
    command = ["whisper-cli", "-m", str(model), "-f", str(wav),
               "-l", "es", "-oj", "-of", str(native_path), "-t", "4"]
    start = time.monotonic()
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=min(4000, duration_ms/1000*2 + 60),
                                pass_fds=() if lock_fd is None else (lock_fd,))
    except subprocess.TimeoutExpired:
        raise ValueError("asr_timeout") from None
    if result.returncode:
        raise ValueError("asr_failed")
    native = json.loads(native_path.with_suffix(".json").read_text())
    segments = []
    for part in native["transcription"]:
        text = part["text"].strip()
        if text:
            start_ms = min(max(part["offsets"]["from"], 0), duration_ms)
            end_ms = min(max(part["offsets"]["to"], start_ms), duration_ms)
            segments.append({"id": f"s{len(segments):06d}", "text": text,
                             "start_ms": start_ms, "end_ms": end_ms, "source_id": source_id})
    transcript = Transcript.model_validate({"language": "es", "model_fingerprint": "whisper:" + MODEL_SHA,
                                           "duration_ms": duration_ms, "segments": segments}).model_dump()
    raw = {"mode": "real", "audio_sha256": file_hash(wav), "model_sha256": MODEL_SHA,
           "sample_rate": 16000, "text": " ".join(s["text"] for s in segments),
           "segments": segments, "elapsed_seconds": time.monotonic()-start}
    (directory / "raw-asr.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    (directory / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=2) + "\n")
    return transcript, raw


def normalize_audio(input_path, output_path, lock_fd=None):
    # Originals are read only. Caller registers output as temporary before conversion.
    result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                             "-y", "-i", str(input_path), "-ac", "1", "-ar", "16000",
                             "-c:a", "pcm_s16le", str(output_path)], capture_output=True, timeout=120,
                             pass_fds=() if lock_fd is None else (lock_fd,))
    if result.returncode:
        raise ValueError("audio_normalization_failed")
