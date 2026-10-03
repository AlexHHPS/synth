"""Hermes agent constrained to a supplied transcript; no tools or persistent memory."""
import json
import os
from pathlib import Path
import sys
import httpx

from run_agent import AIAgent


def main():
    body = json.load(sys.stdin)
    system = "\n".join(m["content"] for m in body["messages"] if m["role"] == "system")
    user = "\n".join(m["content"] for m in body["messages"] if m["role"] == "user")
    agent = AIAgent(
        base_url=os.environ["OMNIROUTE_URL"],
        api_key=os.environ["OMNIROUTE_API_KEY"], provider="custom", api_mode="chat_completions",
        model=os.environ.get("OMNIROUTE_COMBO", "local-combo"), enabled_toolsets=[], max_iterations=2,
        max_tokens=body["max_tokens"], quiet_mode=True,
        skip_context_files=True, skip_memory=True, skip_background_review=True,
        load_soul_identity=False, save_trajectories=False, run_budget_seconds=150,
        request_overrides={"stream": False},
    )
    # Fail closed if upstream changes the meaning of an empty tool allowlist.
    if agent.tools:
        raise ValueError("hermes_tools_unexpected")
    models = []
    original_send = httpx.Client.send
    def observed(client, request, *args, **kwargs):
        response = original_send(client, request, *args, **kwargs)
        if str(request.url) == os.environ["OMNIROUTE_URL"].rstrip("/") + "/chat/completions" and response.is_success:
            response.read()
            if response.headers.get("content-type", "").startswith("application/json"):
                events = [response.json()]
            else:
                events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and line[6:] != "[DONE]"]
            for event in events:
                if event.get("model"):
                    models.append(event["model"])
        return response
    # Hermes creates per-call clients lazily. Observe their transport inside this
    # isolated process, rather than relying on the initial client being reused.
    httpx.Client.send = observed
    try:
        result = agent.run_conversation(user, system_message=system)
        effective_model = models[-1] if models else result.get("served_model")
        if not result.get("completed") or result.get("failed") or result.get("error") or result.get("interrupted") or result.get("partial") or not result.get("final_response") or not effective_model:
            raise ValueError("hermes_incomplete")
        payload = {"model": effective_model, "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": result["final_response"]}}], "orchestrator": "hermes", "requested_combo": os.environ.get("OMNIROUTE_COMBO", "local-combo")}
        Path(os.environ["HERMES_RESULT"]).write_text(json.dumps(payload))
    finally:
        httpx.Client.send = original_send
        agent.close()


if __name__ == "__main__":
    main()
