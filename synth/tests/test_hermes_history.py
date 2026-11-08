"""Hermes keeps the assistant turn of a validator retry (it used to drop it)."""
import io
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class FakeAgent:
    seen = {}

    def __init__(self, **kwargs):
        self.tools = []

    def run_conversation(self, user, system_message=None, conversation_history=None):
        FakeAgent.seen = {"user": user, "system": system_message, "history": conversation_history}
        return {"completed": True, "final_response": "{}", "served_model": "fake-model"}

    def close(self):
        pass


class HermesHistoryTests(unittest.TestCase):
    def run_hermes(self, messages):
        fake_module = types.ModuleType("run_agent")
        fake_module.AIAgent = FakeAgent
        try:
            import httpx  # noqa: F401
        except ImportError:
            self.skipTest("httpx not installed")
        with tempfile.TemporaryDirectory() as home, patch.dict(sys.modules, {"run_agent": fake_module}), \
             patch.dict(os.environ, {"OMNIROUTE_API_KEY": "test", "HERMES_RESULT": home + "/r.json"}), \
             patch("sys.stdin", io.StringIO(json.dumps({"messages": messages, "max_tokens": 1000}))):
            sys.modules.pop("synth.hermes.run", None)
            from synth.hermes import run
            run.main()
            return json.loads(open(home + "/r.json").read())

    def test_retry_passes_the_assistant_turn_as_history(self):
        result = self.run_hermes([{"role": "system", "content": "reglas"}, {"role": "user", "content": "extrae"},
                                  {"role": "assistant", "content": "{malo}"}, {"role": "user", "content": "corrige"}])
        self.assertEqual(FakeAgent.seen["user"], "corrige")
        self.assertEqual(FakeAgent.seen["history"], [{"role": "user", "content": "extrae"}, {"role": "assistant", "content": "{malo}"}])
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")

    def test_first_call_has_no_history(self):
        self.run_hermes([{"role": "system", "content": "reglas"}, {"role": "user", "content": "extrae"}])
        self.assertIsNone(FakeAgent.seen["history"])
        self.assertEqual(FakeAgent.seen["system"], "reglas")


if __name__ == "__main__":
    unittest.main()
