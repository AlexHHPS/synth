"""Substitute acoustic set: synthetic meetings from VoxPopuli `es` (CC0).

Each meeting has a `system` channel (remote participants) and a `microphone`
channel (the operator plus an attenuated, delayed, low-passed echo of the
system channel, i.e. speaker bleed). One in-person meeting has only a
microphone with several speakers. Ground truth (turns, speakers, operator
seconds) comes from the corpus. Deterministic for a given seed and corpus.
"""
from __future__ import annotations

import io
import json
import random
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

RATE = 16_000
LICENSE = "VoxPopuli (facebook/voxpopuli, config es, split test), CC0 1.0"
SOURCE_URL = "https://huggingface.co/datasets/facebook/voxpopuli/resolve/main/es/test-00000-of-00001.parquet"


@dataclass(frozen=True)
class MeetingSpec:
    name: str
    remote_speakers: int
    target_seconds: int
    in_person: bool = False
    echo_delay_ms: int = 80
    echo_db: float = -18.0


SPECS = (
    MeetingSpec("remote-3p-short", 3, 240, echo_delay_ms=40, echo_db=-14.0),
    MeetingSpec("remote-4p-a", 4, 420, echo_delay_ms=80, echo_db=-18.0),
    MeetingSpec("remote-4p-b", 4, 420, echo_delay_ms=120, echo_db=-22.0),
    MeetingSpec("remote-2p-1on1", 2, 300, echo_delay_ms=60, echo_db=-16.0),
    MeetingSpec("remote-5p-long", 5, 600, echo_delay_ms=100, echo_db=-20.0),
    MeetingSpec("in-person-3p", 2, 360, in_person=True),
)


def decode(blob: bytes) -> np.ndarray:
    import soundfile
    data, rate = soundfile.read(io.BytesIO(blob), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if rate != RATE:
        positions = np.linspace(0, len(data) - 1, int(len(data) * RATE / rate))
        data = np.interp(positions, np.arange(len(data)), data).astype("float32")
    return data


def load_corpus(parquet: Path) -> dict[str, list[dict]]:
    import pyarrow.parquet as pq
    table = pq.read_table(parquet, columns=["audio_id", "speaker_id", "raw_text", "audio"])
    by_speaker: dict[str, list[dict]] = {}
    for row in table.to_pylist():
        by_speaker.setdefault(row["speaker_id"], []).append(row)
    for rows in by_speaker.values():
        rows.sort(key=lambda r: r["audio_id"])
    return by_speaker


def write_wav(path: Path, signal: np.ndarray) -> None:
    pcm = (np.clip(signal, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(RATE)
        stream.writeframes(pcm.tobytes())


def echo(system: np.ndarray, delay_ms: int, gain_db: float) -> np.ndarray:
    delayed = np.concatenate([np.zeros(delay_ms * RATE // 1000, "float32"), system])[: len(system)]
    smoothed = np.convolve(delayed, np.ones(4, "float32") / 4, mode="same")
    return smoothed * (10 ** (gain_db / 20))


def choose_speakers(corpus: dict[str, list[dict]], rng: random.Random) -> tuple[str, list[str]]:
    rich = sorted((s for s, rows in corpus.items() if len(rows) >= 20), key=lambda s: (-len(corpus[s]), s))
    operator = rich[0]
    others = sorted(set(corpus) - {operator})
    rng.shuffle(others)
    return operator, others


def build_meeting(spec: MeetingSpec, operator: str, guests: list[str], pools: dict[str, list[dict]],
                  rng: random.Random) -> tuple[dict[str, np.ndarray], dict]:
    participants = [operator] + guests
    tracks = {"microphone": [], "system": []}
    turns, cursor, last = [], 0, None
    while cursor < spec.target_seconds * RATE:
        speaker = rng.choice([p for p in participants if p != last] or participants)
        pool = pools[speaker]
        if not pool:
            participants.remove(speaker)
            if len(participants) < 2:
                break
            continue
        count = min(len(pool), rng.choice((1, 1, 2, 3)))
        clips = [pool.pop(0) for _ in range(count)]
        audio = [decode(c["audio"]["bytes"]) for c in clips]
        gap = np.zeros(int(rng.uniform(0.2, 0.4) * RATE), "float32")
        body = np.concatenate([part for clip in audio for part in (clip, gap)][:-1])
        channel = "microphone" if speaker == operator or spec.in_person else "system"
        start = cursor
        tracks[channel].append((start, body))
        turns.append({"speaker": speaker, "channel": channel, "start_ms": start * 1000 // RATE,
                      "end_ms": (start + len(body)) * 1000 // RATE,
                      "text": " ".join(c["raw_text"] for c in clips), "audio_ids": [c["audio_id"] for c in clips]})
        cursor = start + len(body) + int(rng.uniform(0.3, 0.9) * RATE)
        last = speaker
    length = cursor + RATE
    signals = {}
    for channel, parts in tracks.items():
        signal = np.zeros(length, "float32")
        for start, body in parts:
            signal[start:start + len(body)] += body
        signals[channel] = signal
    if spec.in_person:
        del signals["system"]
    else:
        signals["microphone"] = signals["microphone"] + echo(signals["system"], spec.echo_delay_ms, spec.echo_db)
    noise = np.random.default_rng(rng.randrange(2**32))
    for channel in signals:
        signals[channel] = signals[channel] + noise.normal(0, 0.0015, length).astype("float32")
    per_channel = {c: sorted({t["speaker"] for t in turns if t["channel"] == c}) for c in signals}
    operator_ms = sum(t["end_ms"] - t["start_ms"] for t in turns if t["speaker"] == operator)
    reference = {"name": spec.name, "duration_ms": length * 1000 // RATE, "in_person": spec.in_person,
                 "echo": None if spec.in_person else {"delay_ms": spec.echo_delay_ms, "gain_db": spec.echo_db},
                 "operator": operator, "operator_seconds": operator_ms / 1000,
                 "speakers_per_channel": {c: len(v) for c, v in per_channel.items()},
                 "speaker_ids_per_channel": per_channel, "turns": turns}
    return signals, reference


def build(parquet: Path, output: Path, seed: int = 20261027) -> dict:
    rng = random.Random(seed)
    corpus = load_corpus(parquet)
    operator, others = choose_speakers(corpus, rng)
    operator_rows = list(corpus[operator])
    enrollment = operator_rows[:3]
    pools = {operator: operator_rows[3:]}
    output.mkdir(parents=True, exist_ok=True)
    enrolled = []
    for index, row in enumerate(enrollment):
        path = output / f"operator-enroll-{index}.wav"
        write_wav(path, decode(row["audio"]["bytes"]))
        enrolled.append({"file": path.name, "audio_id": row["audio_id"]})
    meetings = []
    for spec in SPECS:
        guests = [others.pop() for _ in range(spec.remote_speakers)]
        for guest in guests:
            pools[guest] = list(corpus[guest])
        # The operator is the same person in every meeting (as the real user is); each
        # meeting draws its own shuffled copy of the non-enrollment operator clips.
        operator_pool = list(pools[operator])
        rng.shuffle(operator_pool)
        meeting_pools = {operator: operator_pool, **{g: pools[g] for g in guests}}
        signals, reference = build_meeting(spec, operator, guests, meeting_pools, rng)
        folder = output / spec.name
        folder.mkdir(exist_ok=True)
        for channel, signal in signals.items():
            write_wav(folder / f"{channel}.wav", signal)
        (folder / "reference.json").write_text(json.dumps(reference, ensure_ascii=False, indent=1))
        meetings.append(spec.name)
    index = {"license": LICENSE, "source_url": SOURCE_URL, "seed": seed, "operator": operator,
             "enrollment": enrolled, "meetings": meetings}
    (output / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1))
    return index
