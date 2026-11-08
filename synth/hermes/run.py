"""Hermes agent constrained to a supplied transcript; no tools or persistent memory."""
import json
import os
from pathlib import Path
import sys
import threading

from .config import MODELS, gateway
from .content import response_content
from .transport import Observation

from run_agent import AIAgent


def main():
    body = json.load(sys.stdin)
    model = body.get("model", "instant-combo")
    if model not in MODELS:
        raise ValueError("unapproved_combo")
    endpoint = gateway()
    observation = Observation(endpoint)
    streamed = []
    event_lock = threading.Lock()

    def delta(text):
        if not text:
            return
        streamed.append(text)
        with event_lock, open(os.environ["HERMES_EVENTS"], "a", encoding="utf-8") as events:
            events.write(json.dumps({"content": text, "model": observation.model}, ensure_ascii=False) + "\n")
            events.flush()
    system = "\n".join(m["content"] for m in body["messages"] if m["role"] == "system")
    # Keep the conversation: a validator retry carries the assistant's previous answer and the
    # correction request; only the last user message is the new turn.
    dialogue = [m for m in body["messages"] if m["role"] in ("user", "assistant")]
    if not dialogue or dialogue[-1]["role"] != "user":
        raise ValueError("hermes_last_message_must_be_user")
    user = dialogue[-1]["content"]
    history = [{"role": m["role"], "content": m["content"]} for m in dialogue[:-1]]
    return run(body, model, endpoint, observation, delta, streamed, system, user, history)


def run(body, model, endpoint, observation, delta, streamed, system, user, history):
    agent = None
    observation.install()
    try:
        agent = AIAgent(
        base_url=endpoint,
        api_key=os.environ["OMNIROUTE_API_KEY"], provider="custom", api_mode="chat_completions",
        model=model, enabled_toolsets=[], max_iterations=2,
        max_tokens=body["max_tokens"], quiet_mode=True,
        skip_context_files=True, skip_memory=True, skip_background_review=True,
        load_soul_identity=False, save_trajectories=False, run_budget_seconds=150,
        request_overrides={"stream": bool(body.get("stream"))},
        stream_delta_callback=delta if body.get("stream") else None,
        )
        # Fail closed if upstream changes the meaning of an empty tool allowlist.
        if agent.tools:
            raise ValueError("hermes_tools_unexpected")
        result = agent.run_conversation(user, system_message=system, conversation_history=history or None)
        effective_model = observation.model or result.get("served_model")
        if not result.get("completed") or result.get("failed") or result.get("error") or result.get("interrupted") or result.get("partial") or not result.get("final_response") or not effective_model:
            raise ValueError("hermes_incomplete")
        if body.get("stream") and not streamed:
            raise ValueError("hermes_stream_not_contiguous")
        content = response_content(result["final_response"], observation.response_parts,
                                   streamed if body.get("stream") else None)
        payload = {"model": effective_model, "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}], "orchestrator": "hermes", "requested_combo": model,
                   "attempts": observation.attempts, "usage": observation.attempts[-1]["usage"] if observation.attempts else None}
        Path(os.environ["HERMES_RESULT"]).write_text(json.dumps(payload))
    finally:
        observation.close()
        if agent is not None:
            agent.close()


if __name__ == "__main__":
    main()
