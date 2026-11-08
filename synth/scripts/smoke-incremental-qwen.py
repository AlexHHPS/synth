#!/usr/bin/env python3
"""Real Qwen + AEC3 IPC replay over a simulated three-minute source capture.

Uses cached public speech only. Stops its own worker after observing a pending
block, then replays preprocessing and resumes the durable journal. Never records
devices, opens employee profiles, or sends audio to a network service.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import time
from uuid import uuid4
import wave


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    temporary = path.with_suffix(path.suffix+".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def prepare(args):
    root = args.private
    root.mkdir(parents=True, mode=0o700, exist_ok=True); root.chmod(0o700)
    if (root/"manifest.json").exists():
        raise ValueError("replay_already_prepared")
    sources = root/"capture/sources"
    sources.mkdir(parents=True, mode=0o700)
    inputs = {}
    for source in ("microphone", "system"):
        origin = args.corpus/args.meeting/(source+".wav")
        if not origin.is_file():
            continue
        output = sources/(source+".wav.part")
        subprocess.run([str(args.ffmpeg), "-v", "error", "-nostdin", "-i", str(origin), "-t", "180",
                        "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", "-bitexact", str(output)],
                        check=True, capture_output=True)
        output.chmod(0o600)
        with wave.open(str(output)) as audio:
            assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()) == (48000, 1, 2, 180*48000)
        # The native writer uses a plain 44-byte RIFF header, unlike containers
        # with ancillary FFmpeg chunks. Match that protocol without editing PCM.
        with wave.open(str(output)) as audio:
            pcm = audio.readframes(audio.getnframes())
        with wave.open(str(output), "wb") as audio:
            audio.setparams((1, 2, 48000, 0, "NONE", "not compressed")); audio.writeframes(pcm)
        inputs[source] = {"sha256": sha(output), "origin_sha256": sha(origin)}
    save(root/"manifest.json", {"kind": "public_audio_incremental_ipc_replay", "capture_id": str(uuid4()),
        "audio_seconds": 180, "source": "cached assembled/noisy VoxPopuli es test, CC0", "meeting": args.meeting, "inputs": inputs,
        "user_audio_used": False, "capture_clock_simulated": True, "block_seconds": 12,
        "interrupt_before_block": 5, "echo_cancellation": "aec3", "speech_detection": args.speech_detection})
    print(json.dumps({"manifest_sha256": sha(root/"manifest.json"), "audio_seconds": 180}))


def run(args):
    root = args.private.resolve(strict=True)
    manifest = json.loads((root/"manifest.json").read_text())
    appdata = Path.home()/"Library/Application Support/dev.synth.voice"
    config = json.loads((appdata/"asr-config.json").read_text())
    for source, item in manifest["inputs"].items():
        if sha(root/"capture/sources"/(source+".wav.part")) != item["sha256"]:
            raise ValueError("replay_fixture_changed")
    if (root/"state").exists():
        raise ValueError("replay_already_started_inspect_existing_evidence")
    (root/"state").mkdir(mode=0o700)
    lease = os.open(appdata/"pipeline/acoustic-worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    child, stderr, starts, receipts, cursor, visible = None, None, [], [], 0, 0
    runner = args.desktop/"synth/worker/qwen_live_runner.py"
    journal = root/"state/incremental"/manifest["capture_id"]/"blocks.sqlite3"
    def write_request(value):
        child.stdin.write(json.dumps(value)+"\n"); child.stdin.flush()
    def read_response():
        if not select.select([child.stdout], [], [], 120)[0]:
            raise ValueError("replay_response_timeout")
        line = child.stdout.readline(4*1024*1024)
        if not line:
            raise ValueError("replay_worker_exited")
        response = json.loads(line)
        if response.get("error_code"):
            raise ValueError(response["error_code"])
        return response
    def exchange(value):
        write_request(value)
        return read_response()
    def progress(frames, closed=False):
        nonlocal visible
        visible = max(visible, frames)
        save(root/"capture/sources/progress.json", {"version": 1, "capture_id": manifest["capture_id"],
             "state": "closed" if closed else "open", "clock": "recording_elapsed", "sample_rate": 48000,
             "sample_format": "pcm16", "sources": {s: visible for s in manifest["inputs"]}})
    def start():
        nonlocal child, stderr
        stderr = (root/f"worker-{len(starts)}.stderr").open("w"); os.chmod(stderr.name, 0o600)
        began = time.monotonic()
        # The parent and child retain the shared acoustic lease throughout the
        # restart. The child's own state/journal are isolated from the real app.
        child = subprocess.Popen([config["python"], "-I", str(runner)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=stderr, text=True, pass_fds=(lease,), env={**os.environ, "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"})
        ready = exchange({"command": "load", "model": config["asr"], "state": str(root/"state")})
        starts.append({"ready": ready["ready"], "load_seconds": time.monotonic()-began})
        exchange({"command": "capture", "folder": str(root/"capture"), "echo_cancellation": "aec3",
                  "speech_detection": manifest.get("speech_detection", "off")})
    def stop():
        nonlocal child, stderr
        if child is not None:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=10)
            child = None
        if stderr is not None:
            stderr.close(); stderr = None
    def poll():
        nonlocal cursor
        response = exchange({"command": "poll", "capture_id": manifest["capture_id"], "after": cursor})
        events = response.pop("events")
        if events:
            if events[0]["sequence"] != cursor+1 or any(b["sequence"] != a["sequence"]+1 for a,b in zip(events, events[1:])):
                raise ValueError("replay_duplicate_or_missing_events")
            cursor = events[-1]["sequence"]
            with (root/"private-events.jsonl").open("a") as stream:
                os.chmod(stream.name, 0o600)
                for event in events:
                    stream.write(json.dumps(event, ensure_ascii=False)+"\n")
        receipts.append({**response, "new_events": len(events)})
        return response
    try:
        began = time.monotonic(); progress(0); start()
        for block in range(1, 5):
            progress((block*12+1)*48000); poll()
        progress(61*48000)
        write_request({"command": "poll", "capture_id": manifest["capture_id"], "after": cursor})
        deadline, observed_pending = time.monotonic()+5, False
        while time.monotonic()<deadline and child.poll() is None:
            with sqlite3.connect(journal) as db:
                observed_pending = db.execute("SELECT count(*) FROM blocks WHERE state='pending'").fetchone()[0] > 0
            if observed_pending:
                break
            time.sleep(.01)
        stop()
        with sqlite3.connect(journal) as db:
            pending_after_stop = db.execute("SELECT count(*) FROM blocks WHERE state='pending'").fetchone()[0]
        start()
        for block in range(1, 16):
            progress(min(180, block*12+1)*48000, closed=block == 15)
            poll()
        final = poll()
        if final["state"] != "drained":
            raise ValueError("replay_not_drained")
        with sqlite3.connect(journal) as db:
            rows = db.execute("SELECT source,start_frame,end_frame,state,attempts FROM blocks ORDER BY sequence").fetchall()
        complete = len(rows) == 15*len(manifest["inputs"]) and all(r[3] == "complete" for r in rows)
        for source in manifest["inputs"]:
            spans = [(r[1], r[2]) for r in rows if r[0] == source]
            if spans != [(i*12*48000, (i+1)*12*48000) for i in range(15)]:
                raise ValueError("replay_source_coverage_invalid")
        report = {**manifest, "manifest_sha256": sha(root/"manifest.json"), "runner_sha256": sha(runner),
            "incremental_core_sha256": sha(args.desktop/"synth/worker/incremental_audio.py"),
            "verification_tier": "real-model", "complete": complete, "blocks": len(rows), "event_count": cursor,
            "processing_attempts": sum(r[4] for r in rows), "pending_observed_before_interrupt": observed_pending,
            "pending_blocks_after_interrupt": pending_after_stop, "worker_starts": starts,
            "total_seconds": time.monotonic()-began, "peak_mlx_gb": final["peak_mlx_gb"], "api_requests": 0,
            "replay_reused_sources": sum(r.get("reused_sources", 0) for r in receipts),
            "transcription_quality_measured": False, "receipts": receipts,
            "limitations": ["Simulated watermark progression over existing public source files; not recording-device or UI QA.",
                "ASR results are provisional fixed blocks, without boundary consolidation/alignment or speaker identity.",
                "No WER, acta quality or release acceptance claim; 20/60 minute real-model runs remain pending."]}
        save(args.output, report)
        print(json.dumps({k: report[k] for k in ("complete", "blocks", "event_count", "processing_attempts",
              "pending_observed_before_interrupt", "pending_blocks_after_interrupt", "replay_reused_sources", "total_seconds", "peak_mlx_gb")}))
    finally:
        stop(); os.close(lease)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    parser.add_argument("--private", type=Path, required=True)
    parser.add_argument("--desktop", type=Path)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--meeting", default="remote-3p-short")
    parser.add_argument("--speech-detection", choices=("off", "webrtc_rnn"), default="off")
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    {"prepare": prepare, "run": run}[args.action](args)


if __name__ == "__main__":
    main()
