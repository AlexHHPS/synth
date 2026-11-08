#!/usr/bin/env python3
"""Exercise a private Hermes adapter with synthetic text; never save credentials.

HERMES_URL and HERMES_API_KEY come from the caller's environment. This smoke
does not measure meeting quality or claim the 0.5.0 acceptance gate.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import httpx


def check(base, key, route):
    started, first, fragments = time.monotonic(), None, []
    metadata, finished, error = {}, False, None
    try:
        with httpx.Client(timeout=190, trust_env=False) as client:
            with client.stream("POST", base.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + key}, json={
                        "model": route, "stream": True, "max_tokens": 256,
                        "messages": [{"role": "system", "content": "Resume solo lo explícito. Español. Sin herramientas."},
                                     {"role": "user", "content": "Reunión sintética: [t1] La propuesta es lanzar el lunes. [t2] Aún no lo aprobamos. [t3] Acordamos revisar el plan el viernes. Escribe tres líneas: propuesta, decisión y tarea, con sus referencias. No inventes responsable."}],
                    }) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    if line == "data: [DONE]":
                        finished = True
                        break
                    event = json.loads(line[6:])
                    if event.get("error"):
                        error = event["error"]["code"]
                        continue
                    for choice in event.get("choices", []):
                        text = choice.get("delta", {}).get("content")
                        if text:
                            first = first or time.monotonic()
                            fragments.append(text)
                    if event.get("orchestrator"):
                        metadata = event
    except (httpx.HTTPError, ValueError) as exc:
        error = type(exc).__name__
    attempts = metadata.get("attempts") or []
    usage = [attempt.get("usage") for attempt in attempts]
    total_tokens = (sum(u["total_tokens"] for u in usage)
                    if usage and all(isinstance(u, dict) and isinstance(u.get("total_tokens"), int) for u in usage) else None)
    return {"requested_route": route, "verification_tier": "real-model" if finished else "failed-real-model",
            "orchestrator": metadata.get("orchestrator"), "served_model": metadata.get("model"),
            "first_content_ms": round((first - started) * 1000) if first else None,
            "total_ms": round((time.monotonic() - started) * 1000), "content_chunks": len(fragments),
            "content_sha256": hashlib.sha256("".join(fragments).encode()).hexdigest() if fragments else None,
            "usage": metadata.get("usage"), "attempts": attempts, "total_tokens_all_attempts": total_tokens,
            "cost_usd": None, "completed": finished, "error": error}


def main():
    base, key = os.environ["HERMES_URL"], os.environ["HERMES_API_KEY"]
    results = []
    for route in ("local-combo", "cheap-combo"):
        value = check(base, key, route)
        results.append(value)
        print(json.dumps(value), flush=True)
    report = {"purpose": "adapter-stream-smoke", "synthetic_input": True,
              "gateway": os.environ.get("OMNIROUTE_URL"), "results": results}
    if len(sys.argv) > 1:
        output = Path(sys.argv[1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if all(v["completed"] and v["content_chunks"] > 1 and v["orchestrator"] == "hermes"
                    and v["served_model"] not in (None, "local-combo", "cheap-combo") for v in results) else 1


if __name__ == "__main__":
    sys.exit(main())
