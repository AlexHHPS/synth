"""Hermes answers 429 while its single lane is busy: wait it out, never fail a job or a question for that alone."""
import io
import unittest
import urllib.error

from synth.worker import omniroute


class Opener:
    def __init__(self, codes):
        self.codes, self.calls = list(codes), 0

    def open(self, request, timeout):
        self.calls += 1
        code = self.codes.pop(0)
        if code != 200:
            raise urllib.error.HTTPError("http://hermes", code, "busy", {}, io.BytesIO(b""))
        return io.BytesIO(b'{"ok": true}')


class BusyGatewayTests(unittest.TestCase):
    def test_busy_lane_is_retried_until_free(self):
        waits = []
        opener = Opener([429, 429, 200])
        self.assertEqual(omniroute._send(opener, object(), sleep=waits.append), b'{"ok": true}')
        self.assertEqual((opener.calls, waits), (3, [omniroute.BUSY_WAIT_S] * 2))

    def test_other_errors_are_not_retried(self):
        opener = Opener([503, 200])
        with self.assertRaises(urllib.error.HTTPError):
            omniroute._send(opener, object(), sleep=lambda s: None)
        self.assertEqual(opener.calls, 1)

    def test_a_lane_busy_for_too_long_still_fails(self):
        opener = Opener([429] * (omniroute.BUSY_RETRIES + 1))
        with self.assertRaises(urllib.error.HTTPError):
            omniroute._send(opener, object(), sleep=lambda s: None)
        self.assertEqual(opener.calls, omniroute.BUSY_RETRIES + 1)


if __name__ == "__main__":
    unittest.main()
