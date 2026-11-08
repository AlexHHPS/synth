#!/usr/bin/env python3
"""One synthetic real-model request through the worker's streaming client."""
import hashlib
import json
from pathlib import Path
import sys
import time
from synth.worker.omniroute import completion

started, arrived, chunks = time.monotonic(), [], []
def delta(text):
    arrived.append(time.monotonic())
    chunks.append(text)

result = completion([
    {"role": "system", "content": "Español. Solo hechos explícitos y sus referencias. Sin herramientas."},
    {"role": "user", "content": "Reunión sintética: [t1] Proponemos lanzar el lunes, aún sin aprobarlo. [t2] Acordamos revisar el plan el viernes. Resume en dos frases con sus referencias. No asignes responsable."},
], max_tokens=512, route="cheap-combo", on_delta=delta)
finished = time.monotonic()
report = {"purpose": "worker-stream-smoke", "synthetic_input": True, "verification_tier": "real-model",
          "requested_route": result["requested_combo"], "served_model": result["effective_model"],
          "orchestrator": result["orchestrator"], "gateway": "http://127.0.0.1:20128/v1",
          "first_content_ms": round((arrived[0] - started) * 1000) if arrived else None,
          "total_ms": round((finished - started) * 1000), "content_chunks": len(chunks),
          "received_before_completion": bool(arrived and arrived[0] < finished),
          "content_sha256": hashlib.sha256(result["content"].encode()).hexdigest(),
          "usage": result["response"].get("usage"), "attempts": result["response"].get("attempts"),
          "cost_usd": None}
Path(sys.argv[1]).write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
if len(chunks) < 2 or "".join(chunks) != result["content"]:
    raise SystemExit("WORKER_STREAM_SMOKE_FAILED")
