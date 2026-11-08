"""Observe the isolated Hermes process without consuming its upstream stream.

Each response is an attempt, including failed/fallback attempts. Usage snapshots
replace earlier snapshots from that response; cumulative usage is never summed
per chunk. An absent cost or an unpriced zero remains unknown.
"""
import json
import time
from urllib.parse import urlsplit

import httpx


class ObservedStream(httpx.SyncByteStream):
    def __init__(self, upstream, observe):
        self.upstream, self.observe = upstream, observe
        self.pending = b""

    def __iter__(self):
        for chunk in self.upstream:
            self.pending += chunk
            while b"\n" in self.pending:
                line, self.pending = self.pending.split(b"\n", 1)
                self.line(line.rstrip(b"\r"))
            if len(self.pending) > 1_000_000:
                raise ValueError("gateway_event_too_large")
            yield chunk
        if self.pending:
            self.line(self.pending.rstrip(b"\r"))

    def line(self, line):
        if line.startswith(b"data:"):
            raw = line[5:].strip()
            if raw and raw != b"[DONE]":
                self.observe(json.loads(raw))

    def close(self):
        self.upstream.close()


class Observation:
    def __init__(self, gateway):
        self.endpoint = urlsplit(gateway)
        self.attempts = []
        self.visible_parts = []  # Private; never included in public usage metadata.

    @property
    def response_parts(self):
        return ["".join(parts) for parts in self.visible_parts]

    @property
    def model(self):
        return next((a["served_model"] for a in reversed(self.attempts)
                     if a["served_model"] and a["status"] is not None and 200 <= a["status"] < 300), None)

    def install(self):
        self.original = httpx.Client.send
        observation = self

        def send(client, request, *args, **kwargs):
            # This hook exists only inside a private, tools-disabled subprocess.
            if (request.url.host != observation.endpoint.hostname or
                    request.url.port != observation.endpoint.port or
                    request.url.path != observation.endpoint.path.rstrip("/") + "/chat/completions"):
                return observation.original(client, request, *args, **kwargs)
            attempt = {"status": None, "served_model": None, "provider": None,
                       "request_id": None, "usage": None, "cost_usd": None,
                       "reported_cost": None, "cache_hit": None, "fallback_attempts": None,
                       "error": None, "response_headers_ms": None}
            observation.attempts.append(attempt)
            visible = []
            observation.visible_parts.append(visible)
            started = time.monotonic()
            try:
                response = observation.original(client, request, *args, **kwargs)
            except httpx.HTTPError as error:
                attempt["error"] = type(error).__name__
                raise
            attempt["response_headers_ms"] = round((time.monotonic() - started) * 1000)
            headers = response.headers
            attempt.update({"status": response.status_code, "served_model": headers.get("x-omniroute-model"),
                       "provider": headers.get("x-omniroute-provider"),
                       "request_id": headers.get("x-omniroute-request-id"),
                       "usage": None, "cost_usd": None,
                       "reported_cost": headers.get("x-omniroute-response-cost"),
                       "cache_hit": headers.get("x-omniroute-cache-hit"),
                       "fallback_attempts": headers.get("x-omniroute-fallback-attempts")})

            def event(value):
                if not isinstance(value, dict):
                    return
                if value.get("model") and not headers.get("x-omniroute-model"):
                    attempt["served_model"] = value["model"]
                if isinstance(value.get("usage"), dict):
                    attempt["usage"] = value["usage"]
                for choice in value.get("choices", [])[:1]:
                    if not isinstance(choice, dict):
                        continue
                    text = (choice.get("delta") or choice.get("message") or {}).get("content")
                    if isinstance(text, str):
                        visible.append(text)

            if headers.get("content-type", "").startswith("text/event-stream"):
                # Do not response.read(): Hermes must receive each delta now.
                response.stream = ObservedStream(response.stream, event)
            elif response.is_success and headers.get("content-type", "").startswith("application/json"):
                response.read()
                event(response.json())
            return response

        httpx.Client.send = send
        return self

    def close(self):
        httpx.Client.send = self.original
