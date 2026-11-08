"""Per-meeting whisper prompt (≤ 40 approved terms) with VAD; silence yields no hallucinated terms."""
import shutil
import tempfile
import unittest
import wave
from pathlib import Path

from synth.worker import asr


class PromptTests(unittest.TestCase):
    def test_prompt_is_a_natural_sentence_with_at_most_40_unique_terms(self):
        prompt = asr.glossary_prompt([f"Término{i}" for i in range(60)] + ["Término1"])
        self.assertTrue(prompt.startswith("En esta reunión se habla de Término0, Término1"))
        self.assertTrue(prompt.endswith(" y Término39."))
        self.assertEqual(prompt.count("Término"), 40)
        self.assertIsNone(asr.glossary_prompt([]))
        self.assertEqual(asr.glossary_prompt(["Valdivar"]), "En esta reunión se habla de Valdivar.")


@unittest.skipUnless(shutil.which("whisper-cli") and asr.MODEL.is_file() and asr.VAD_MODEL.is_file(), "whisper runtime not installed")
class SilenceWithPromptTests(unittest.TestCase):
    def test_silence_with_glossary_prompt_produces_no_terms(self):
        import random
        prompt = asr.glossary_prompt(["Valdivar", "Lucerno", "Synth", "Nimbus", "OmniRoute", "Cloudflare"])
        rng = random.Random(0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "silence.wav"
            with wave.open(str(path), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(16000)
                stream.writeframes(b"".join(int(rng.gauss(0, 60)).to_bytes(2, "little", signed=True) for _ in range(16000 * 8)))
            transcript, raw = asr.transcribe(path, Path(directory) / "out", prompt=prompt)
        self.assertTrue(raw["vad"])
        self.assertEqual(raw["prompt"], prompt)
        text = " ".join(s["text"] for s in transcript["segments"])
        for term in ("Valdivar", "Lucerno", "Synth", "Nimbus", "OmniRoute", "Cloudflare"):
            self.assertNotIn(term, text)

class MeetingPromptTests(unittest.TestCase):
    def test_folder_terms_first_and_api_failure_means_no_prompt(self):
        from synth.worker.desktop_pipeline import meeting_prompt

        class API:
            def __init__(self, fail=False):
                self.fail, self.paths = fail, []

            def call(self, method, path):
                self.paths.append(path)
                if self.fail:
                    raise ValueError("api_unavailable")
                return {"items": [{"term": "Nimbus", "scope": "workspace", "occurrences": 9},
                                  {"term": "Valdivar", "scope": "folder", "occurrences": 1}]}
        api = API()
        self.assertEqual(meeting_prompt(api, "f1"), "En esta reunión se habla de Valdivar y Nimbus.")
        self.assertEqual(api.paths, ["/v1/glossary?status=aprobado&folder_id=f1"])
        self.assertIsNone(meeting_prompt(API(fail=True)))


if __name__ == "__main__":
    unittest.main()
