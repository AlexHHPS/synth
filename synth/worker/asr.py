"""Native Whisper adapter (whisper.cpp large-v3-turbo q5_0, hash pinned).

The output of this module is the raw ASR transcript and is never edited
afterwards: glossary corrections create a separate transcript version, and WER benchmarks are always measured on this raw output. The only
domain input is an optional per-meeting prompt listing approved glossary
terms (≤ 40, a natural sentence, with VAD on so silence yields no text).
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import wave

from synth.contracts.transcript import Transcript
from .journal import file_hash
from synth.worker.subprocesses import run_process

MODEL_SHA = "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin"
# The packaged host sets SYNTH_ASR_MODEL to the copy inside the app; development uses the pinned
# download prepared by synth/scripts/prepare-runtime.py.
RUNTIME_MODEL = Path(__file__).resolve().parents[2] / "synth/.runtime/models/ggml-large-v3-turbo-q5_0.bin"
MODEL = Path(os.environ["SYNTH_ASR_MODEL"]) if os.environ.get("SYNTH_ASR_MODEL") else RUNTIME_MODEL
# Silero VAD for whisper.cpp (ggml-org/whisper-vad, MIT). Optional: absent ⇒ no VAD flags.
VAD_MODEL = Path(os.environ["SYNTH_VAD_MODEL"]) if os.environ.get("SYNTH_VAD_MODEL") else Path(__file__).resolve().parents[2] / "synth/.runtime/models/vad/ggml-silero-v6.2.0.bin"
VAD_SHA = "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987"
_VERIFIED = set()


def vad_flags(model=None):
    model = Path(model or VAD_MODEL)
    if not model.is_file() or file_hash(model) != VAD_SHA:
        return []
    return ["--vad", "-vm", str(model), "-vsd", "300", "-vp", "200"]


MAX_PROMPT_TERMS = 40


def glossary_prompt(terms):
    """«En esta reunión se habla de A, B y C.» with at most 40 unique approved terms, or None."""
    unique = []
    for term in terms:
        term = " ".join(str(term).split())
        if term and term.casefold() not in {u.casefold() for u in unique} and len(term) <= 60:
            unique.append(term)
    unique = unique[:MAX_PROMPT_TERMS]
    if not unique:
        return None
    listed = unique[0] if len(unique) == 1 else ", ".join(unique[:-1]) + " y " + unique[-1]
    return f"En esta reunión se habla de {listed}."


def transcribe(wav, working_directory, source_id="import", model=MODEL, lock_fd=None, vad_model=None, prompt=None):
    from .qwen_asr import CONFIG, transcribe as transcribe_qwen
    if CONFIG.is_file():
        configuration = json.loads(CONFIG.read_text())
        if configuration.get('backend') == 'qwen3':
            return transcribe_qwen(wav, working_directory, source_id, lock_fd, configuration, prompt=prompt)
        if configuration.get('backend') != 'whisper':
            raise ValueError('asr_backend_invalid')
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
               "-l", "es", "-ojf", "-of", str(native_path), "-t", "4", *vad_flags(vad_model),
               *(["--prompt", prompt] if prompt else [])]
    start = time.monotonic()
    try:
        result = run_process(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
    transcript = {"language": "es", "model_fingerprint": "whisper:" + MODEL_SHA, "duration_ms": duration_ms, "segments": segments}
    # With VAD a silent source yields no segments; only a non-empty source is a valid Transcript on its own.
    if segments:
        transcript = Transcript.model_validate(transcript).model_dump()
    from .turns import words_from_whisper
    raw = {"mode": "real", "audio_sha256": file_hash(wav), "model_sha256": MODEL_SHA,
           "sample_rate": 16000, "text": " ".join(s["text"] for s in segments),
           "segments": segments, "words": words_from_whisper(native, duration_ms, source_id),
           "vad": bool(vad_flags(vad_model)), "prompt": prompt, "elapsed_seconds": time.monotonic()-start}
    (directory / "raw-asr.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    (directory / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=2) + "\n")
    return transcript, raw


def normalize_audio(input_path, output_path, lock_fd=None):
    # Originals are read only. Caller registers output as temporary before conversion.
    try:
        result = run_process(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                                 "-y", "-i", str(input_path), "-ac", "1", "-ar", "16000",
                                 "-c:a", "pcm_s16le", str(output_path)], capture_output=True, timeout=120,
                                 pass_fds=() if lock_fd is None else (lock_fd,))
    except subprocess.TimeoutExpired:
        raise ValueError('audio_normalization_timeout') from None
    if result.returncode:
        raise ValueError("audio_normalization_failed")
