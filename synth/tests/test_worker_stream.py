"""The worker consumes real SSE framing; partial streams never get retried."""
import io
import json
import os
import unittest
from unittest.mock import patch
from synth.worker import omniroute


class Opener:
    def __init__(self, wire):
        self.wire, self.calls = wire, 0
    def open(self, request, timeout):
        self.calls += 1
        self.request = request
        return self.wire


def wire(text="Decisión: revisar el viernes.", done=True, finish="stop"):
    values = [{"model": "served-model", "choices": [{"delta": {"content": fragment}, "finish_reason": None}]}
              for fragment in [text[:10], text[10:]]]
    values.append({"model": "served-model", "orchestrator": "hermes", "attempts": [{"usage": {"total_tokens": 40}}],
                   "choices": [{"delta": {}, "finish_reason": finish}]})
    return ("\r\n".join("data: " + json.dumps(v, ensure_ascii=False) + "\r\n" for v in values)
            + ("data: [DONE]\r\n\r\n" if done else "")).encode()


class WorkerStreamTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"OMNIROUTE_URL": "http://127.0.0.1:20128/v1",
                           "HERMES_URL": "http://127.0.0.1:18294/v1", "HERMES_API_KEY": "synthetic"}))

    def test_deltas_are_forwarded_before_the_response_is_finished(self):
        source = io.BytesIO(wire())
        received = []
        def delta(value):
            self.assertLess(source.tell(), len(source.getvalue()))
            received.append(value)
        opener = Opener(source)
        with patch("urllib.request.build_opener", return_value=opener):
            result = omniroute.completion([{"role": "user", "content": "Synthetic input"}], route="cheap-combo", on_delta=delta)
        self.assertEqual("".join(received), "Decisión: revisar el viernes.")
        self.assertEqual(result["content"], "".join(received))
        self.assertEqual(result["requested_combo"], "cheap-combo")
        self.assertEqual(result["orchestrator"], "hermes")
        self.assertEqual(result["response"]["attempts"][0]["usage"]["total_tokens"], 40)
        self.assertTrue(json.loads(opener.request.data)["stream"])

    def test_disconnect_after_partial_output_is_an_error_and_does_not_replay_inference(self):
        opener, received = Opener(io.BytesIO(wire(done=False))), []
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "gateway_stream_incomplete"):
                omniroute.completion([], on_delta=received.append)
        self.assertTrue(received)
        self.assertEqual(opener.calls, 1)

    def test_length_limit_and_error_event_never_finish_a_generation(self):
        for data, error in [(wire(finish="length"), "gateway_output_incomplete"),
                            (b'data: {"error":{"code":"synthetic"}}\n\n', "gateway_stream_failed")]:
            with patch("urllib.request.build_opener", return_value=Opener(io.BytesIO(data))):
                with self.assertRaisesRegex(ValueError, error):
                    omniroute.completion([], on_delta=lambda value: None)

    def test_cancellation_from_the_persistence_callback_stops_reading(self):
        opener = Opener(io.BytesIO(wire()))
        def fenced(_):
            raise ValueError("generation_fenced")
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "generation_fenced"):
                omniroute.completion([], on_delta=fenced)
        self.assertEqual(opener.calls, 1)

    def test_without_hermes_the_gateway_is_called_directly_with_its_own_key(self):
        opener = Opener(io.BytesIO(wire()))
        with patch.dict(os.environ, {"HERMES_URL": "", "OMNIROUTE_URL": "https://gateway.example.com/v1",
                                     "OMNIROUTE_API_KEY": "gateway-key"}), \
             patch("urllib.request.build_opener", return_value=opener):
            result = omniroute.completion([], on_delta=lambda value: None)
        self.assertEqual(opener.request.full_url, "https://gateway.example.com/v1/chat/completions")
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer gateway-key")
        self.assertEqual(result["orchestrator"], "gateway")

    def test_unapproved_hermes_is_rejected_before_any_request(self):
        with patch.dict(os.environ, {"HERMES_URL": "http://evil.example.com/v1"}), patch("urllib.request.build_opener") as network:
            with self.assertRaisesRegex(ValueError, "unapproved_hermes"):
                omniroute.completion([])
            network.assert_not_called()

if __name__ == "__main__":
    unittest.main()
