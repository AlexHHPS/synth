"""Transport timing, real subprocess lifetime and bounded request concurrency.

The fake model is intentional: these checks prove the adapter, not model quality.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from synth.hermes.transport import Observation
from synth.hermes import app as adapter


class TransportTests(unittest.TestCase):
    def test_network_failure_is_an_attempt_with_unknown_usage_and_no_private_error_text(self):
        def fail(request):
            raise httpx.ConnectError("private upstream details", request=request)
        observation = Observation("http://127.0.0.1:20128/v1").install()
        try:
            with httpx.Client(transport=httpx.MockTransport(fail)) as client:
                with self.assertRaises(httpx.ConnectError):
                    client.post("http://127.0.0.1:20128/v1/chat/completions")
            self.assertEqual(len(observation.attempts), 1)
            self.assertEqual(observation.attempts[0]["error"], "ConnectError")
            self.assertIsNone(observation.attempts[0]["usage"])
            self.assertIsNone(observation.model)
            self.assertNotIn("private upstream", json.dumps(observation.attempts))
        finally:
            observation.close()

    def test_sse_is_observed_as_consumed_without_eager_read(self):
        state = {"finished": False}

        class Wire(httpx.SyncByteStream):
            def __iter__(self):
                # Deliberately split UTF-8 and JSON over arbitrary network frames.
                raw = 'data: {"model":"real-model","choices":[{"delta":{"content":"sí"}}]}\n\n'.encode()
                yield raw[:20]
                yield raw[20:]
                state["finished"] = True
                yield b'data: {"usage":{"prompt_tokens":12,"completion_tokens":4}}\n\n'
                yield b'data: [DONE]\n\n'

        original = httpx.Client.send
        observation = Observation("http://127.0.0.1:20128/v1").install()
        try:
            with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200,
                    headers={"content-type": "text/event-stream"}, stream=Wire()))) as client:
                with client.stream("POST", "http://127.0.0.1:20128/v1/chat/completions") as response:
                    self.assertFalse(state["finished"], "observer buffered the upstream")
                    frames = response.iter_bytes()
                    self.assertTrue(next(frames))
                    self.assertIsNone(observation.model)
                    self.assertTrue(next(frames))
                    self.assertEqual(observation.model, "real-model")
                    self.assertFalse(state["finished"])
                    list(frames)
            self.assertEqual(observation.attempts[0]["usage"], {"prompt_tokens": 12, "completion_tokens": 4})
            self.assertIsNone(observation.attempts[0]["cost_usd"])
        finally:
            observation.close()
        self.assertIs(httpx.Client.send, original)

    def test_fallback_attempts_and_latest_usage_are_not_lost_or_double_counted(self):
        responses = [httpx.Response(429), httpx.Response(200,
                     headers={"content-type": "application/json", "x-omniroute-model": "served",
                              "x-omniroute-response-cost": "0"},
                     json={"model": "cheap-combo", "usage": {"total_tokens": 24}})]
        observation = Observation("http://127.0.0.1:20128/v1").install()
        try:
            with httpx.Client(transport=httpx.MockTransport(lambda req: responses.pop(0))) as client:
                for _ in range(2):
                    client.post("http://127.0.0.1:20128/v1/chat/completions")
            self.assertEqual([a["status"] for a in observation.attempts], [429, 200])
            self.assertEqual(observation.model, "served")
            self.assertEqual(observation.attempts[1]["usage"]["total_tokens"], 24)
            self.assertIsNone(observation.attempts[1]["cost_usd"])
        finally:
            observation.close()


CHILD = r'''
import json, os, sys, time
from pathlib import Path
body=json.load(sys.stdin)
marker=Path(os.environ["TEST_HERMES_MARKERS"])/str(os.getpid())
marker.write_text("started")
delay=0.8 if body["messages"][-1]["content"]=="slow" else 0.03
text=["Decisión: ","revisar el viernes."]
for fragment in text:
    with open(os.environ["HERMES_EVENTS"],"a") as f:
        f.write(json.dumps({"content":fragment,"model":"fixture-model"})+"\n"); f.flush()
    time.sleep(delay)
Path(os.environ["HERMES_RESULT"]).write_text(json.dumps({"model":"fixture-model",
    "choices":[{"finish_reason":"stop","message":{"content":"".join(text)}}],"orchestrator":"hermes"}))
'''


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.enterContext(patch.dict(os.environ, {"HERMES_API_KEY": "test", "TEST_HERMES_MARKERS": self.directory.name}))
        self.enterContext(patch.object(adapter, "COMMAND", [sys.executable, "-c", CHILD]))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=adapter.app),
                                      base_url="http://test", headers={"Authorization": "Bearer test"})

    async def asyncTearDown(self):
        await self.client.aclose()

    def body(self, content="fast", stream=False, model="cheap-combo"):
        return {"model": model, "messages": [{"role": "user", "content": content}], "stream": stream}

    async def test_two_calls_run_and_third_is_bounded_without_blocking_the_event_loop(self):
        tasks = [asyncio.create_task(self.client.post("/v1/chat/completions", json=self.body("slow"))) for _ in range(2)]
        try:
            deadline = time.monotonic() + 3
            while len(list(Path(self.directory.name).iterdir())) < 2:
                self.assertLess(time.monotonic(), deadline)
                await asyncio.sleep(0.01)
            self.assertTrue(all(not task.done() for task in tasks))
            busy = await self.client.post("/v1/chat/completions", json=self.body())
            self.assertEqual(busy.status_code, 429)
            self.assertEqual((await self.client.get("/health")).status_code, 200)
            results = await asyncio.gather(*tasks)
            self.assertEqual([r.status_code for r in results], [200, 200])
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_first_fragment_arrives_while_subprocess_is_running_and_close_terminates_it(self):
        # Directly consume the response body: ASGI test transports buffer streams.
        request = httpx.Request("POST", "http://test", headers={"Authorization": "Bearer test"})
        response = await adapter.completion(adapter.Completion(**self.body("slow", stream=True)), request)
        iterator = response.body_iterator
        first = await anext(iterator)
        self.assertIn("Decisión", first)
        pid = int(next(Path(self.directory.name).iterdir()).name)
        os.kill(pid, 0)
        await iterator.aclose()
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        recovered = await self.client.post("/v1/chat/completions", json=self.body())
        self.assertEqual(recovered.status_code, 200)

    async def test_completed_stream_matches_final_result_and_finishes_once(self):
        response = await self.client.post("/v1/chat/completions", json=self.body(stream=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count("data: [DONE]"), 1)
        values = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        self.assertEqual("".join(v["choices"][0]["delta"].get("content", "") for v in values),
                         "Decisión: revisar el viernes.")
        self.assertEqual(values[-1]["choices"][0]["finish_reason"], "stop")

    async def test_timeout_releases_process_and_capacity(self):
        with patch.object(adapter, "PROCESS_TIMEOUT", 0.1):
            result = await self.client.post("/v1/chat/completions", json=self.body("slow"))
        self.assertEqual(result.status_code, 504)
        pid = int(next(Path(self.directory.name).iterdir()).name)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual((await self.client.post("/v1/chat/completions", json=self.body())).status_code, 200)

    async def test_auth_and_route_validation_happen_before_starting_process(self):
        self.assertEqual((await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer wrong"}, json=self.body())).status_code, 401)
        self.assertEqual((await self.client.post("/v1/chat/completions", json=self.body(model="unapproved"))).status_code, 400)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    async def test_non_text_and_invalid_conversation_are_rejected_before_inference(self):
        for messages, status in [([{"role": "user", "content": {"audio": "unapproved"}}], 422),
                                 ([{"role": "tool", "content": "tool output"}], 422),
                                 ([{"role": "assistant", "content": "unfinished"}], 400)]:
            result = await self.client.post("/v1/chat/completions", json={"model": "cheap-combo", "messages": messages})
            self.assertEqual(result.status_code, status)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
