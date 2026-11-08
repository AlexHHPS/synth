"""Private Hermes adapter. Each call gets an isolated process and temporary home."""
import hmac
import asyncio
import json
import logging
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import signal
import time
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from .config import MODELS, capacity

app = FastAPI(title="Synth Hermes")
LOG = logging.getLogger("synth.hermes")
ERROR_NAME = re.compile(r"\b([A-Z][A-Za-z]+(?:Error|Exception|Timeout))\b")
STATUS = re.compile(r"(?:status(?:_code)?|Error code|HTTP)[\s:=]+([1-5]\d\d)\b")


def failure_code(stderr: str) -> str:
    """Exception class and HTTP status only: never message text, prompts or keys."""
    names = ERROR_NAME.findall(stderr or "")
    statuses = STATUS.findall(stderr or "")
    return (names[-1] if names else "unknown") + (" status=" + statuses[-1] if statuses else "")
LANES = threading.BoundedSemaphore(capacity())
COMMAND = [sys.executable, "-m", "synth.hermes.run"]
PROCESS_TIMEOUT = 170


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=1_000_000)


class Completion(BaseModel):
    model: str
    messages: list[Message] = Field(min_length=1, max_length=64)
    max_tokens: int = Field(default=4096, ge=1, le=8192)
    temperature: float = 0
    stream: bool = False


@app.get("/health")
def health():
    return {"status": "ready", "orchestrator": "hermes", "models": sorted(MODELS), "capacity": capacity()}


class Invocation:
    """One bounded subprocess. Its files are private and last only for this call."""
    def __init__(self, body):
        self.home = tempfile.TemporaryDirectory(prefix="synth-hermes-")
        root = Path(self.home.name)
        self.result_path, self.events_path = root / "result.json", root / "events.jsonl"
        self.events_path.touch(mode=0o600)
        self.stderr = open(root / "stderr", "w+b")
        self.process = None
        self.deadline = time.monotonic() + PROCESS_TIMEOUT
        try:
            self.process = subprocess.Popen(COMMAND, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=self.stderr, start_new_session=True,
                env={**os.environ, "HERMES_HOME": str(root), "HERMES_RESULT": str(self.result_path),
                     "HERMES_EVENTS": str(self.events_path)})
            self.process.stdin.write(json.dumps(body.model_dump()).encode())
            self.process.stdin.close()
        except BaseException:
            self.close()
            raise

    def check_timeout(self):
        if time.monotonic() >= self.deadline:
            raise HTTPException(504, "Hermes timeout")

    def result(self):
        if self.process.returncode or not self.result_path.exists():
            self.stderr.seek(0, 2)
            self.stderr.seek(max(0, self.stderr.tell() - 8192))
            LOG.warning("hermes_failed code=%s returncode=%s",
                        failure_code(self.stderr.read().decode(errors="replace")), self.process.returncode)
            raise HTTPException(502, "Hermes processing failed")
        if self.result_path.stat().st_size > 16 * 1024 * 1024:
            raise HTTPException(502, "Hermes response too large")
        return json.loads(self.result_path.read_text())

    def close(self):
        if self.process:
            # The child may have descendants holding HTTP connections or file descriptors.
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=2)
        self.stderr.close()
        self.home.cleanup()


def sse(value):
    return "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"


async def stream(invocation):
    identifier = "chatcmpl-" + uuid4().hex
    pending = ""
    delivered = []
    try:
        with open(invocation.events_path, encoding="utf-8") as events:
            while True:
                invocation.check_timeout()
                done = invocation.process.poll() is not None
                pending += events.read()
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if not line:
                        continue
                    value = json.loads(line)
                    delivered.append(value["content"])
                    yield sse({"id": identifier, "object": "chat.completion.chunk", "model": value.get("model"),
                               "choices": [{"index": 0, "delta": {"content": value["content"]}, "finish_reason": None}]})
                if len(pending) > 1_000_000:
                    raise HTTPException(502, "Hermes event too large")
                if done:
                    break
                await asyncio.sleep(0.02)
        result = invocation.result()
        if pending or "".join(delivered) != result["choices"][0]["message"]["content"]:
            raise HTTPException(502, "Hermes stream incomplete")
        yield sse({**{k: v for k, v in result.items() if k != "choices"}, "id": identifier,
                   "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        yield "data: [DONE]\n\n"
    except HTTPException as error:
        yield sse({"error": {"code": "hermes_stream_failed", "status": error.status_code}})
    except (ValueError, OSError, KeyError, IndexError):
        yield sse({"error": {"code": "hermes_stream_invalid", "status": 502}})
    finally:
        try:
            await asyncio.to_thread(invocation.close)
        finally:
            LANES.release()


@app.post("/v1/chat/completions")
async def completion(body: Completion, request: Request):
    expected = "Bearer " + os.environ["HERMES_API_KEY"]
    if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
        raise HTTPException(401, "Unauthorized")
    if body.model not in MODELS:
        raise HTTPException(400, "Unsupported model")
    if body.messages[-1].role != "user":
        raise HTTPException(400, "Last message must be user")
    if len(json.dumps(body.model_dump())) > 1_000_000:
        raise HTTPException(413, "Request too large")
    if not LANES.acquire(blocking=False):
        raise HTTPException(429, "Hermes busy")
    invocation = None
    streaming = False
    try:
        start = asyncio.create_task(asyncio.to_thread(Invocation, body))
        try:
            invocation = await asyncio.shield(start)
        except asyncio.CancelledError:
            # Cancelling a thread future doesn't stop Popen. Obtain its handle
            # before cleanup so a disconnected caller cannot leak a child.
            invocation = await start
            raise
        if body.stream:
            response = StreamingResponse(stream(invocation), media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
            streaming = True
            return response
        while invocation.process.poll() is None:
            invocation.check_timeout()
            await asyncio.sleep(0.02)
        return invocation.result()
    finally:
        if not streaming:
            try:
                if invocation is not None:
                    await asyncio.to_thread(invocation.close)
            finally:
                LANES.release()
