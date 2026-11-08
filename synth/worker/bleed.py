"""Microphone bleed control: the mic picks up what the speakers play.

1. Acoustic gate (`gate_microphone`): compare 30 ms energy envelopes of mic and
   system at the best lag; mute mic frames whose energy is explained by the
   system echo alone. Frames with local speech above the echo stay untouched.
2. Text dedup (`drop_echo_words`): after ASR, microphone words that repeat
   nearby system speech (word trigrams within ±2 s) are removed as echo.
"""
from __future__ import annotations

import bisect
import re
import unicodedata
import wave
from pathlib import Path
from typing import Any

import numpy as np

RATE = 16_000
FRAME = 480  # 30 ms
MAX_LAG_FRAMES = 20  # 600 ms
LOCAL_MARGIN_DB = 6.0
ACTIVE_MARGIN_DB = 10.0


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as stream:
        if stream.getframerate() != RATE or stream.getnchannels() != 1 or stream.getsampwidth() != 2:
            raise ValueError("bleed_input_must_be_mono_16khz_pcm16")
        return np.frombuffer(stream.readframes(stream.getnframes()), "<i2").astype("float32") / 32768


def write_wav(path: Path, signal: np.ndarray) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(RATE)
        stream.writeframes((np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes())


def envelope(signal: np.ndarray) -> np.ndarray:
    frames = len(signal) // FRAME
    if not frames:
        return np.zeros(0, "float64")
    blocks = signal[: frames * FRAME].reshape(frames, FRAME)
    return 10 * np.log10(np.einsum("ij,ij->i", blocks, blocks, dtype="float64") / FRAME + 1e-10)


def best_lag(mic: np.ndarray, system: np.ndarray, system_db: np.ndarray, windows: int = 5) -> int:
    """Echo delay in samples by GCC-PHAT on the loudest system windows (median over windows)."""
    size = 8 * RATE
    max_lag = MAX_LAG_FRAMES * FRAME
    if len(system_db) * FRAME < size or len(mic) < size:
        return 0
    per_window = size // FRAME
    loudness = [system_db[i:i + per_window].mean() for i in range(0, len(system_db) - per_window, per_window)]
    lags = []
    for index in sorted(range(len(loudness)), key=lambda i: -loudness[i])[:windows]:
        lo = index * per_window * FRAME
        a, b = mic[lo:lo + size].astype("float64"), system[lo:lo + size].astype("float64")
        spectrum = np.fft.rfft(a, 2 * size) * np.conj(np.fft.rfft(b, 2 * size))
        correlation = np.fft.irfft(spectrum / (np.abs(spectrum) + 1e-12))
        candidates = correlation[: max_lag + 1]
        lags.append(int(np.argmax(candidates)))
    return int(np.median(lags)) if lags else 0


def gate_microphone(mic: np.ndarray, system: np.ndarray) -> tuple[np.ndarray, dict[str, Any]]:
    mic_db, system_db = envelope(mic), envelope(system)
    n = min(len(mic_db), len(system_db))
    if not n:
        return mic.copy(), {"lag_ms": 0, "echo_gain_db": None, "gated_fraction": 0.0}
    lag_samples = best_lag(mic, system, system_db)
    lag = int(round(lag_samples / FRAME))
    shifted = np.full(n, -100.0)
    shifted[lag:] = system_db[: n - lag]
    floor = np.percentile(system_db[:n], 20)
    active = shifted > floor + ACTIVE_MARGIN_DB
    if active.sum() < 10:
        return mic.copy(), {"lag_ms": lag_samples // 16, "echo_gain_db": None, "gated_fraction": 0.0}
    gain = float(np.median(mic_db[:n][active] - shifted[active]))
    predicted = shifted + gain
    echo_only = active & (mic_db[:n] < predicted + LOCAL_MARGIN_DB)
    # Frame-level gain with a one-frame crossfade, expanded per sample only once (no per-sample convolution).
    gain_per_frame = np.where(echo_only, 0.0, 1.0).astype("float32")
    smoothed = np.minimum(gain_per_frame, np.convolve(gain_per_frame, np.ones(3, "float32") / 3, mode="same"))
    gated = mic.copy()
    gated[: n * FRAME] *= np.repeat(smoothed, FRAME)
    return gated, {"lag_ms": lag_samples // 16, "echo_gain_db": round(gain, 2),
                        "gated_fraction": round(float(echo_only.sum()) / n, 4)}


def gate_file(mic_path: Path, system_path: Path, output: Path) -> dict[str, Any]:
    gated, stats = gate_microphone(read_wav(mic_path), read_wav(system_path))
    write_wav(output, gated)
    return stats


def _norm(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    parts = re.findall(r"[\w-]+", plain)
    return parts[0] if parts else ""


def _window(index: list[int], items: list, lo: int, hi: int) -> list:
    """Items whose start falls in [lo, hi]; `index` is the sorted list of their starts."""
    return items[bisect.bisect_left(index, lo):bisect.bisect_right(index, hi)]


def drop_echo_words(mic_words: list[dict[str, Any]], system_words: list[dict[str, Any]],
                    window_ms: int = 2000, n: int = 3) -> tuple[list[dict[str, Any]], int]:
    """Remove microphone words that belong to a word n-gram also spoken nearby on the system channel."""
    if not mic_words or not system_words:
        return list(mic_words), 0
    system = sorted(system_words, key=lambda w: w["start_ms"])
    starts = [w["start_ms"] for w in system]
    system_norm = [(_norm(w["text"]), w["start_ms"], w["end_ms"]) for w in system]
    mic_norm = [_norm(w["text"]) for w in mic_words]
    echo = [False] * len(mic_words)
    for i in range(len(mic_words) - n + 1):
        gram = tuple(mic_norm[i:i + n])
        if not all(gram):
            continue
        lo, hi = mic_words[i]["start_ms"] - window_ms, mic_words[i + n - 1]["end_ms"] + window_ms
        near = [t for t, _, _ in _window(starts, system_norm, lo - window_ms, hi)]
        grams = {tuple(near[j:j + n]) for j in range(len(near) - n + 1)}
        if gram in grams:
            for j in range(i, i + n):
                echo[j] = True
    _flag_echo_runs(mic_words, mic_norm, starts, system_norm, echo, window_ms)
    kept = [w for w, flagged in zip(mic_words, echo) if not flagged]
    return kept, sum(echo)


FUNCTION_WORDS = frozenset("""a al de del el la las lo los en y o u que se si no es eso esto esa ese un una unos unas
con por para pero ya yo tu te me mi le les nos su sus mas muy pues bueno vale claro aqui ahi ahora asi""".split())


def _flag_echo_runs(mic_words, mic_norm, starts, system_norm, echo, window_ms,
                    share=0.6, overlap=0.5, gap_ms=1500, min_content=3):
    """Fuzzy pass for echo the ASR heard differently on each channel («Nimbul» vs «Nimbus»):
    a microphone run of ≥ 3 content words, spoken while the system channel was speaking, whose
    content words mostly occur in the nearby system text, is echo. Short replies («sí, eso es»)
    and answers in the system's silences are kept."""
    runs, current = [], []
    for index, word in enumerate(mic_words):
        if current and word["start_ms"] - mic_words[current[-1]]["end_ms"] >= gap_ms:
            runs.append(current)
            current = []
        current.append(index)
    if current:
        runs.append(current)
    for run in runs:
        lo, hi = mic_words[run[0]]["start_ms"], mic_words[run[-1]]["end_ms"]
        content = [mic_norm[i] for i in run if mic_norm[i] and mic_norm[i] not in FUNCTION_WORDS]
        if len(content) < min_content:
            continue
        nearby = _window(starts, system_norm, lo - 2 * window_ms, hi + window_ms)
        near = {t for t, start, end in nearby if end >= lo - window_ms}
        if sum(t in near for t in content) / len(content) < share:
            continue
        spoken = sum(max(0, min(end, hi + 1000) - max(start, lo - 1000)) for _, start, end in nearby
                     if end >= lo - 1000 and start <= hi + 1000)
        if spoken / max(1, hi - lo + 2000) >= overlap:
            for i in run:
                echo[i] = True
