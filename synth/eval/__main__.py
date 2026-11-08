"""python3 -m synth.eval {build-corpus|purge}: synthetic corpus and opt-in retention maintenance."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "synth/.runtime/eval-corpus/synthetic"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m synth.eval")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-corpus", help="synthetic meetings from a public VoxPopuli parquet")
    build.add_argument("--parquet", type=Path, default=ROOT / "synth/.runtime/eval-corpus/voxpopuli-es-test.parquet")
    purge = commands.add_parser("purge", help="erase retained evaluation audio")
    purge.add_argument("--keep-key", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "build-corpus":
        from .corpus import build as build_corpus
        print(json.dumps(build_corpus(args.parquet, CORPUS), ensure_ascii=False)[:300])
        return 0
    from . import retention
    removed = retention.purge(forget_key=None if args.keep_key else retention.forget_keychain_key)
    print(f"purged {removed} eval meetings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
