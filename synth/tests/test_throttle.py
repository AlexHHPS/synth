import unittest
from types import SimpleNamespace

from synth.server import throttle


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        throttle.reset(); self.addCleanup(throttle.reset)

    def test_repeated_refusals_block_one_address_for_a_minute_and_nobody_else(self):
        for index in range(throttle.REFUSALS):
            self.assertFalse(throttle.blocked("203.0.113.9", now=index))
            throttle.refused("203.0.113.9", now=index)
        self.assertTrue(throttle.blocked("203.0.113.9", now=throttle.REFUSALS))
        self.assertFalse(throttle.blocked("203.0.113.10", now=throttle.REFUSALS))
        self.assertFalse(throttle.blocked("203.0.113.9", now=throttle.REFUSALS + throttle.WINDOW_SECONDS))

    def test_the_desktop_host_on_loopback_is_never_blocked(self):
        for _ in range(throttle.REFUSALS + 5):
            throttle.refused("127.0.0.1")
        self.assertFalse(throttle.blocked("127.0.0.1"))

    def test_the_address_is_the_one_the_proxy_saw_not_the_one_the_caller_claims(self):
        request = SimpleNamespace(headers={"x-forwarded-for": "10.0.0.1, 198.51.100.7"}, client=SimpleNamespace(host="100.64.0.2"))
        self.assertEqual(throttle.client(request), "198.51.100.7")
        self.assertEqual(throttle.client(SimpleNamespace(headers={}, client=SimpleNamespace(host="100.64.0.2"))), "100.64.0.2")
