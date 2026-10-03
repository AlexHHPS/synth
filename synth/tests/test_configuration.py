import unittest
from synth.config import origin


class DeploymentConfigurationTests(unittest.TestCase):
    def test_https_origin_and_explicit_loopback_only(self):
        self.assertEqual(origin('https://voice.example.com'), 'https://voice.example.com')
        self.assertEqual(origin('http://127.0.0.1:18280', loopback=True), 'http://127.0.0.1:18280')
        for value in ['http://voice.example.com', 'https://voice.example.com@evil.invalid',
                      'https://voice.example.com/path', 'https://voice.example.com?redirect=evil',
                      'https://voice.example.com#fragment', '//evil.invalid', 'https://']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                origin(value, loopback=True)
