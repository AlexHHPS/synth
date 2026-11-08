"""Text inference through the operator-configured OmniRoute gateway, optionally via Hermes.

Never bypass the gateway or silently replace its combo. No raw audio/profiles.
"""
import json
import logging
import os
import time
from pathlib import Path
import urllib.error
import urllib.request
from synth.hermes.config import LOCAL_GATEWAYS, MODELS, approved_endpoint

POLICY_ID = "operator-configured-gateway-v2"
COMBO = os.environ.get("OMNIROUTE_COMBO", "local-combo")


BUSY_RETRIES, BUSY_WAIT_S = 12, 10


def _open(opener, request, sleep=time.sleep):
    """Retry busy admission only, before reading any content. Never replay a partial stream."""
    for attempt in range(BUSY_RETRIES + 1):
        try:
            return opener.open(request, timeout=180)
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == BUSY_RETRIES:
                raise
            sleep(BUSY_WAIT_S)
    raise ValueError("gateway_unreachable")


def _send(opener, request, sleep=time.sleep):
    with _open(opener, request, sleep) as response:
        return response.read(16 * 1024 * 1024 + 1)


def _stream(opener, request, on_delta):
    content, result, size, stopped, done = [], {}, 0, False, False
    with _open(opener, request) as response:
        while True:
            line = response.readline(1_000_001)
            if not line:
                break
            size += len(line)
            if len(line) > 1_000_000 or size > 16 * 1024 * 1024:
                raise ValueError("gateway_response_too_large")
            if not line.startswith(b"data:"):
                continue
            raw = line[5:].strip()
            if raw == b"[DONE]":
                done = True
                break
            event = json.loads(raw)
            if event.get("error"):
                raise ValueError("gateway_stream_failed")
            if stopped:
                raise ValueError("gateway_stream_after_stop")
            for choice in event.get("choices", []):
                fragment = choice.get("delta", {}).get("content")
                if fragment:
                    if not isinstance(fragment, str):
                        raise ValueError("gateway_output_invalid")
                    content.append(fragment)
                    on_delta(fragment)
                if choice.get("finish_reason"):
                    if choice["finish_reason"] != "stop":
                        raise ValueError("gateway_output_incomplete")
                    stopped = True
            result.update({k: v for k, v in event.items() if k not in ("choices", "id", "object")})
    if not done or not stopped:
        raise ValueError("gateway_stream_incomplete")
    result["choices"] = [{"finish_reason": "stop", "message": {"content": "".join(content)}}]
    return result


def completion(messages, max_tokens=8192, *, route=None, on_delta=None):
    route = route or COMBO
    gateway = os.environ.get("OMNIROUTE_URL", "http://127.0.0.1:20128/v1").rstrip("/")
    if not approved_endpoint(gateway, LOCAL_GATEWAYS):
        raise ValueError("unapproved_gateway")
    if route not in MODELS:
        raise ValueError("unapproved_combo")
    # Hermes is optional: when configured, every call goes through it; otherwise straight to the gateway.
    hermes = os.environ.get("HERMES_URL", "").rstrip("/")
    if hermes and not approved_endpoint(hermes, frozenset({"http://127.0.0.1:18294/v1", "http://hermes:18284/v1"})):
        raise ValueError("unapproved_hermes")
    url = hermes or gateway
    prefix = "HERMES" if hermes else "OMNIROUTE"
    key_file = os.environ.get(prefix + "_API_KEY_FILE")
    key = Path(key_file).read_text().strip() if key_file else os.environ.get(prefix + "_API_KEY", "")
    if not key:
        raise ValueError("gateway_key_missing")
    request = urllib.request.Request(url + "/chat/completions", data=json.dumps({
        "model": route, "messages": messages, "temperature": 0,
        "max_tokens": max_tokens, "stream": on_delta is not None,
    }, ensure_ascii=False).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    # No environment HTTP proxy or redirects to an unapproved destination.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        if on_delta is not None:
            result = _stream(opener, request, on_delta)
        else:
            payload = _send(opener, request)
            if len(payload) > 16 * 1024 * 1024:
                raise ValueError("gateway_response_too_large")
            result = json.loads(payload)
        if hermes and result.get("orchestrator") != "hermes":
            raise ValueError("gateway_orchestrator_missing")
        choice = result["choices"][0]
        if choice.get("finish_reason") not in ("stop", None):
            raise ValueError("gateway_output_incomplete")
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip() or not result.get("model"):
            raise ValueError("gateway_output_missing")
    except (OSError, KeyError, IndexError, json.JSONDecodeError) as error:
        status = getattr(error, "code", None)
        logging.getLogger("synth.worker").warning("gateway_error cause=%s%s", type(error).__name__,
                                                   f" status={status}" if isinstance(status, int) else "")
        raise ValueError("gateway_unavailable_or_invalid") from error
    return {"mode": "real", "runtime_policy_id": POLICY_ID,
            "routed_via_omniroute": True, "requested_combo": route,
            "orchestrator": "hermes" if hermes else "gateway",
            "effective_model": result["model"], "content": content,
            "response": result}
