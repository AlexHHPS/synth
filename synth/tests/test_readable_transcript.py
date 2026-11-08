import json
import unittest

from synth.contracts.transcript import Transcript
from synth.worker.desktop_pipeline import canonical_transcript, diarize_sources
from synth.worker.transcript_quality import label_live


def asr(source, items, duration=20_000):
    segments, words = [], []
    for index, (text, start, end) in enumerate(items):
        segments.append({"id": f"s{index:06d}", "text": text, "start_ms": start, "end_ms": end, "source_id": source})
        step = (end - start) // max(1, len(text.split()))
        for k, token in enumerate(text.split()):
            words.append({"text": token, "start_ms": start + k * step, "end_ms": start + (k + 1) * step - 10,
                          "source_id": source, "segment_index": index})
    return {"language": "es", "model_fingerprint": "whisper:test", "duration_ms": duration, "segments": segments}, words


class RemoteMeetingTests(unittest.TestCase):
    def setUp(self):
        self.system, system_words = asr("system", [
            ("¿Quieres que preparemos la apertura de una ronda de financiación?", 1000, 5000),
            ("Perfecto, entonces lo vemos el viernes con el comité", 9000, 13000)])
        self.mic, mic_words = asr("microphone", [
            ("quieres que preparemos la apertura", 1100, 4000),
            ("sí la preparo yo esta semana con los números", 5600, 8500)])
        self.diarizations = {"system": {"speech_state": "detected", "segments": [
            {"start_ms": 1000, "end_ms": 5000, "speaker_id": "S1"}, {"start_ms": 9000, "end_ms": 13000, "speaker_id": "S2"}]},
            "microphone": {"speech_state": "local_user", "segments": []}}
        self.words = {"system": system_words, "microphone": mic_words}

    def build(self):
        return canonical_transcript({"system": self.system, "microphone": self.mic}, self.diarizations, self.words)

    def test_microphone_echo_segment_is_removed_and_local_answer_kept(self):
        result = self.build()
        texts = [s["text"] for s in result["segments"]]
        self.assertNotIn("quieres que preparemos la apertura", texts)
        self.assertIn("sí la preparo yo esta semana con los números", texts)

    def test_microphone_is_the_local_user(self):
        result = self.build()
        local = [t for t in result["turns"] if t["source_id"] == "microphone"]
        self.assertEqual({t["speaker_id"] for t in local}, {"microphone:local"})
        self.assertNotIn("quieres que preparemos", " ".join(t["text"] for t in local))

    def test_turns_reference_existing_segments_for_old_citations(self):
        result = self.build()
        ids = {s["id"] for s in result["segments"]}
        self.assertTrue(all(t["segment_ids"] and set(t["segment_ids"]) <= ids for t in result["turns"]))
        self.assertEqual([t["speaker_id"] for t in result["turns"]], ["system:S1", "microphone:local", "system:S2"])

    def test_remote_microphone_is_not_diarized(self):
        calls = []
        import synth.worker.desktop_pipeline as pipeline
        original = pipeline.infer
        pipeline.infer = lambda op, path, raw, lock_fd=None: calls.append(path) or {
            "speech_state": "detected", "segments": [{"start_ms": 0, "end_ms": 1000, "speaker_id": "S1"}]}
        try:
            result = diarize_sources({"microphone": {"path": "mic.wav"}, "system": {"path": "sys.wav"}}, "raw", lambda: None)
        finally:
            pipeline.infer = original
        self.assertEqual(calls, ["sys.wav"])
        self.assertEqual(result["microphone"]["speech_state"], "local_user")

    def test_new_capture_diarizes_both_channels_even_with_remote_speech(self):
        from unittest.mock import patch
        import synth.worker.desktop_pipeline as pipeline
        calls = []
        def infer(operation, path, raw, lock_fd=None):
            calls.append(path)
            return {'speech_state': 'detected', 'segments': [{'speaker_id': 'S1', 'start_ms': 0, 'end_ms': 1000}]}
        with patch.object(pipeline, 'infer', side_effect=infer):
            result = diarize_sources({'microphone': {'path': 'mic.wav'}, 'system': {'path': 'sys.wav'}},
                                     'raw', lambda: None, independent_channels=True)
        self.assertEqual(calls, ['sys.wav', 'mic.wav'])
        self.assertEqual(result['microphone']['speech_state'], 'detected')

    def test_new_capture_does_not_remove_words_only_because_both_channels_agree(self):
        text = 'lo que acabamos de revisar'
        microphone, microphone_words = asr('microphone', [(text, 0, 3000)])
        system, system_words = asr('system', [(text, 0, 3000)])
        diarizations = {source: {'speech_state': 'detected', 'segments': [
            {'speaker_id': 'S1', 'start_ms': 0, 'end_ms': 3000}]} for source in ('microphone', 'system')}
        result = canonical_transcript({'microphone': microphone, 'system': system}, diarizations,
            {'microphone': microphone_words, 'system': system_words}, [])
        self.assertEqual([s['text'] for s in result['segments']], [text, text])
        self.assertEqual({s['speaker_id'] for s in result['segments']}, {'microphone:S1', 'system:S1'})

    def test_in_person_microphone_is_diarized(self):
        calls = []
        import synth.worker.desktop_pipeline as pipeline
        original = pipeline.infer
        pipeline.infer = lambda op, path, raw, lock_fd=None: calls.append(path) or {"speech_state": "detected", "segments": []}
        try:
            diarize_sources({"microphone": {"path": "mic.wav"}}, "raw", lambda: None)
        finally:
            pipeline.infer = original
        self.assertEqual(calls, ["mic.wav"])


class ContractTests(unittest.TestCase):
    base = {"language": "es", "model_fingerprint": "x", "duration_ms": 5000,
            "segments": [{"id": "s000000", "text": "hola", "start_ms": 0, "end_ms": 1000}]}

    def test_transcript_without_turns_serializes_exactly_as_before(self):
        dumped = Transcript.model_validate(self.base).model_dump()
        self.assertNotIn("turns", dumped)
        self.assertNotIn('asr_evidence', dumped)
        self.assertNotIn('asr_block_id', dumped['segments'][0])
        self.assertNotIn('evidence_state', dumped['segments'][0])

    def test_asr_block_metadata_requires_the_original_hypothesis(self):
        source = json.loads(json.dumps(self.base))
        source['segments'][0].update(asr_block_id='11111111-1111-4111-8111-111111111111:system:0',
                                     evidence_state='needs_review')
        with self.assertRaisesRegex(ValueError, 'asr_evidence_missing'):
            Transcript.model_validate(source)

    def test_turn_citing_a_missing_segment_is_rejected(self):
        bad = {**self.base, "turns": [{"id": "t000000", "text": "hola", "start_ms": 0, "end_ms": 1000,
                                       "segment_ids": ["s999999"], "word_count": 1}]}
        with self.assertRaises(ValueError):
            Transcript.model_validate(bad)

    def test_turns_round_trip(self):
        good = {**self.base, "turns": [{"id": "t000000", "text": "hola", "start_ms": 0, "end_ms": 1000,
                                        "speaker_id": "microphone:local", "segment_ids": ["s000000"], "word_count": 1}]}
        self.assertEqual(Transcript.model_validate(good).model_dump()["turns"][0]["segment_ids"], ["s000000"])


class LiveFallbackTests(unittest.TestCase):
    def test_live_fallback_gets_per_track_speakers_and_turns(self):
        live = {"language": "es", "model_fingerprint": "capture-live:x", "duration_ms": 20000, "segments": [
            {"id": "s000000", "text": "¿Preparamos la ronda?", "start_ms": 1000, "end_ms": 4000, "source_id": "live_mix",
             "speaker_id": None, "employee_id": None},
            {"id": "s000001", "text": "Sí, la preparo yo", "start_ms": 5600, "end_ms": 8000, "source_id": "live_mix",
             "speaker_id": None, "employee_id": None}]}
        diarizations = {"system": {"segments": [{"start_ms": 1000, "end_ms": 4500, "speaker_id": "S1"}]}}
        words = {"microphone": [{"text": "sí", "start_ms": 5700, "end_ms": 6000},
                                {"text": "la", "start_ms": 6000, "end_ms": 6300},
                                {"text": "preparo", "start_ms": 6300, "end_ms": 7000}]}
        result = label_live(json.loads(json.dumps(live)), diarizations, words)
        self.assertEqual([s["speaker_id"] for s in result["segments"]], ["system:S1", "microphone:local"])
        self.assertEqual(len(result["turns"]), 2)
        self.assertTrue(all(s["speaker_id"] for s in result["segments"]))

    def test_live_fallback_without_diarization_stays_unknown(self):
        live = {"language": "es", "model_fingerprint": "capture-live:x", "duration_ms": 5000, "segments": [
            {"id": "s000000", "text": "hola", "start_ms": 0, "end_ms": 1000, "source_id": "live_mix",
             "speaker_id": None, "employee_id": None}]}
        result = label_live(live, {}, {})
        self.assertIsNone(result["segments"][0]["speaker_id"])
        self.assertNotIn("turns", result)



class ReviewRegressionTests(unittest.TestCase):
    def test_short_local_reply_over_remote_speech_is_kept(self):
        from synth.worker.bleed import drop_echo_words
        system = [{"text": w, "start_ms": 1000 + i * 300, "end_ms": 1250 + i * 300}
                  for i, w in enumerate("y eso es lo que vamos a revisar en el comité".split())]
        reply = [{"text": w, "start_ms": 2000 + i * 300, "end_ms": 2250 + i * 300} for i, w in enumerate("sí, eso es".split())]
        kept, removed = drop_echo_words(reply, system)
        self.assertEqual(removed, 0)
        self.assertEqual(len(kept), 3)

    def test_in_person_dominant_microphone_is_local_in_segments_and_turns(self):
        mic, words = asr("microphone", [("hoy reviso yo el informe del trimestre", 0, 4000)])
        diarization = {"microphone": {"speech_state": "detected", "segments": [{"start_ms": 0, "end_ms": 4000, "speaker_id": "S1"}]}}
        result = canonical_transcript({"microphone": mic}, diarization, {"microphone": words})
        self.assertEqual({s["speaker_id"] for s in result["segments"]}, {"microphone:local"})
        self.assertEqual({t["speaker_id"] for t in result["turns"]}, {"microphone:local"})

    def test_in_person_shared_microphone_names_the_same_speakers_in_segments_and_turns(self):
        mic, words = asr("microphone", [("hoy reviso yo el informe", 0, 4000), ("y yo preparo los números del trimestre", 5000, 9000)])
        diarization = {"microphone": {"speech_state": "detected", "segments": [
            {"start_ms": 0, "end_ms": 4000, "speaker_id": "S1"}, {"start_ms": 5000, "end_ms": 9000, "speaker_id": "S2"}]}}
        result = canonical_transcript({"microphone": mic}, diarization, {"microphone": words})
        self.assertEqual({s["speaker_id"] for s in result["segments"]}, {"microphone:S1", "microphone:S2"})
        self.assertEqual({t["speaker_id"] for t in result["turns"]}, {"microphone:S1", "microphone:S2"})

    def test_remote_microphone_with_two_voices_is_local_in_segments_and_turns(self):
        system, system_words = asr("system", [("Perfecto, entonces lo vemos el viernes con el comité", 12000, 16000)])
        mic, mic_words = asr("microphone", [("hoy reviso yo el informe", 0, 4000), ("y yo preparo los números del trimestre", 5000, 9000)])
        diarization = {"system": {"speech_state": "detected", "segments": [{"start_ms": 12000, "end_ms": 16000, "speaker_id": "S1"}]},
                       "microphone": {"speech_state": "detected", "segments": [
                           {"start_ms": 0, "end_ms": 4000, "speaker_id": "S1"}, {"start_ms": 5000, "end_ms": 9000, "speaker_id": "S2"}]}}
        result = canonical_transcript({"system": system, "microphone": mic}, diarization, {"system": system_words, "microphone": mic_words})
        mic_turns = {t["speaker_id"] for t in result["turns"] if t["speaker_id"].startswith("microphone")}
        mic_segments = {s["speaker_id"] for s in result["segments"] if s["source_id"] == "microphone"}
        self.assertEqual(mic_turns, {"microphone:local"})
        self.assertEqual(mic_segments, {"microphone:local"})


if __name__ == "__main__":
    unittest.main()
