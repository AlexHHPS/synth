#!/usr/bin/env python3
"""Operate the app's durable host pipeline without exposing its bearer key."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from synth.worker.desktop_queue import DesktopQueue
from synth.worker.desktop_pipeline import CAPTURE, STATE, work_once


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("--audio", type=Path, required=True)
    enqueue.add_argument("--source", choices=["import", "microphone", "system"], default="import")
    enqueue.add_argument("--title", required=True)
    enqueue.add_argument("--folder-id")
    enqueue.add_argument("--consent-confirmed", action="store_true")
    commands.add_parser("once")
    commands.add_parser("watch")
    for command in ("status", "retry", "cancel"):
        commands.add_parser(command).add_argument("task_id")
    args = parser.parse_args()
    CAPTURE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = STATE / "queue.sqlite3"
    if args.command in {"once", "watch"}:
        while True:
            task = work_once(database, CAPTURE, STATE)
            if task:
                print(json.dumps({"id": task["id"], "state": task["state"],
                    "stage": task["stage"], "error_code": task["error_code"],
                    "result": task["result"]}), flush=True)
            if args.command == "once":
                return
            time.sleep(1)
    queue = DesktopQueue(database, CAPTURE)
    try:
        if args.command == "enqueue":
            task = queue.enqueue({args.source: args.audio}, args.title, args.folder_id, args.consent_confirmed)
        else:
            task = getattr(queue, {"status": "get", "retry": "retry", "cancel": "cancel"}[args.command])(args.task_id)
        # No source paths, titles, transcript text or credentials in command output.
        print(json.dumps({"id": task["id"], "state": task["state"], "stage": task["stage"],
                          "error_code": task["error_code"]}))
    finally:
        queue.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError):
        print("host_pipeline_command_failed; inspect task status", file=sys.stderr)
        sys.exit(1)
