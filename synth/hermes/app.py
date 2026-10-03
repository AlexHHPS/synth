"""Private Hermes adapter. Each call gets an isolated process and temporary home."""
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

app = FastAPI(title="Synth Hermes")
LANE = threading.Lock()


class Completion(BaseModel):
    model: str
    messages: list[dict] = Field(min_length=1, max_length=10)
    max_tokens: int = Field(default=4096, ge=1, le=8192)
    temperature: float = 0
    stream: bool = False


@app.get("/health")
def health():
    return {"status": "ready", "orchestrator": "hermes", "combo": os.environ.get("OMNIROUTE_COMBO", "local-combo")}


@app.post("/v1/chat/completions")
def completion(body: Completion, request: Request):
    expected = "Bearer " + os.environ["HERMES_API_KEY"]
    if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
        raise HTTPException(401, "Unauthorized")
    if body.model != os.environ.get("OMNIROUTE_COMBO", "local-combo") or body.stream:
        raise HTTPException(400, "Unsupported model or stream")
    if len(json.dumps(body.model_dump())) > 1_000_000:
        raise HTTPException(413, "Request too large")
    if not LANE.acquire(blocking=False):
        raise HTTPException(429, "Hermes busy")
    try:
        with tempfile.TemporaryDirectory(prefix="synth-hermes-") as home:
            result_path = Path(home) / "result.json"
            env = {**os.environ, "HERMES_HOME": home, "HERMES_RESULT": str(result_path)}
            process = subprocess.run(
                [sys.executable, "-m", "synth.hermes.run"],
                input=json.dumps(body.model_dump()), text=True, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=170,
            )
            if process.returncode or not result_path.exists():
                raise HTTPException(502, "Hermes processing failed")
            return json.loads(result_path.read_text())
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Hermes timeout") from None
    finally:
        LANE.release()
