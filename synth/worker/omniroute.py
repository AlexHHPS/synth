"""Existing user-authorized local-combo; provider may be external.

Never bypass the gateway or silently replace its combo. No raw audio/profiles.
"""
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

POLICY_ID = "operator-configured-gateway-v1"
COMBO = os.environ.get("OMNIROUTE_COMBO", "local-combo")


def completion(messages, max_tokens=4096):
    gateway = os.environ.get("OMNIROUTE_URL", "http://127.0.0.1:20128/v1").rstrip("/")
    from urllib.parse import urlsplit
    parsed = urlsplit(gateway)
    local = gateway in ("http://127.0.0.1:20128/v1", "http://host.docker.internal:20128/v1")
    if not local and (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != "/v1"):
        raise ValueError("unapproved_gateway")
    if not COMBO or len(COMBO) > 128:
        raise ValueError("unapproved_combo")
    hermes = os.environ.get("HERMES_URL", "").rstrip("/")
    if hermes:
        hp = urlsplit(hermes)
        if hp.username or hp.password or hp.query or hp.fragment or hp.path != "/v1" or not hp.hostname or (hp.scheme != "https" and hermes != "http://hermes:18284/v1"):
            raise ValueError("unapproved_hermes")
    url = hermes or gateway
    key_file = os.environ.get("OMNIROUTE_API_KEY_FILE")
    key = Path(key_file).read_text().strip() if key_file else os.environ.get("OMNIROUTE_API_KEY", "")
    if hermes:
        key = os.environ.get("HERMES_API_KEY", "")
    if not key:
        raise ValueError("gateway_key_missing")
    request = urllib.request.Request(url + "/chat/completions", data=json.dumps({
        "model": COMBO, "messages": messages, "temperature": 0,
        "max_tokens": max_tokens, "stream": False,
    }, ensure_ascii=False).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    # No environment HTTP proxy or redirects to an unapproved destination.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=180) as response:
            payload = response.read(16 * 1024 * 1024 + 1)
        if len(payload) > 16 * 1024 * 1024:
            raise ValueError("gateway_response_too_large")
        result = json.loads(payload)
        choice = result["choices"][0]
        if choice.get("finish_reason") not in ("stop", None):
            raise ValueError("gateway_output_incomplete")
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip() or not result.get("model"):
            raise ValueError("gateway_output_missing")
    except (OSError, KeyError, IndexError, json.JSONDecodeError) as error:
        raise ValueError("gateway_unavailable_or_invalid") from error
    return {"mode": "real", "runtime_policy_id": POLICY_ID,
            "routed_via_omniroute": True, "requested_combo": COMBO,
            "orchestrator": "hermes" if hermes else "direct_worker",
            "effective_model": result["model"], "content": content,
            "response": result}
