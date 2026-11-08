import unittest

from synth.worker import turns


def word(text, start, end, speaker, source="system"):
    return {"text": text, "start_ms": start, "end_ms": end, "speaker_id": speaker, "source_id": source}


class WhisperWordsTests(unittest.TestCase):
    def test_subword_tokens_are_glued_and_specials_skipped(self):
        native = {"transcription": [{"tokens": [
            {"text": "[_BEG_]", "offsets": {"from": 0, "to": 0}},
            {"text": " respons", "offsets": {"from": 300, "to": 600}},
            {"text": "ables", "offsets": {"from": 600, "to": 900}},
            {"text": " tienen", "offsets": {"from": 900, "to": 1300}},
            {"text": ",", "offsets": {"from": 1300, "to": 1310}}]}]}
        words = turns.words_from_whisper(native, 5000, "system")
        self.assertEqual([w["text"] for w in words], ["responsables", "tienen,"])
        self.assertEqual((words[0]["start_ms"], words[0]["end_ms"]), (300, 900))


class LabelTests(unittest.TestCase):
    diarization = {"segments": [{"start_ms": 0, "end_ms": 1000, "speaker_id": "S1"},
                                {"start_ms": 1000, "end_ms": 3000, "speaker_id": "S2"}]}

    def test_word_takes_speaker_with_most_overlap(self):
        labelled = turns.label_words([word("hola", 800, 1400, None)], self.diarization, "system")
        self.assertEqual(labelled[0]["speaker_id"], "system:S2")

    def test_word_outside_any_turn_takes_nearest_within_tolerance(self):
        labelled = turns.label_words([word("vale", 3200, 3400, None)], self.diarization, "system")
        self.assertEqual(labelled[0]["speaker_id"], "system:S2")

    def test_remote_microphone_is_the_local_user(self):
        labelled = turns.label_words([word("yo", 0, 300, None, "microphone")], {"segments": []}, "microphone",
                                     local_channel=True)
        self.assertEqual(labelled[0]["speaker_id"], "microphone:local")

    def test_shared_microphone_without_dominant_voice_keeps_diarized_labels(self):
        shared = {"segments": [{"start_ms": 0, "end_ms": 1000, "speaker_id": "S1"},
                               {"start_ms": 1000, "end_ms": 2000, "speaker_id": "S2"}]}
        labelled = turns.label_words([word("tú", 1200, 1500, None, "microphone")], shared, "microphone",
                                     local_channel=True)
        self.assertEqual(labelled[0]["speaker_id"], "microphone:S2")


class BuildTurnsTests(unittest.TestCase):
    def test_same_speaker_with_short_pauses_is_one_turn(self):
        words = [word("uno", 0, 300, "A"), word("dos", 1200, 1500, "A"), word("tres", 2600, 2900, "A")]
        result = turns.build_turns(words)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "uno dos tres")
        self.assertEqual(result[0]["id"], "t000000")

    def test_long_pause_splits_turn(self):
        words = [word("uno", 0, 300, "A"), word("dos", 2500, 2800, "A")]
        self.assertEqual(len(turns.build_turns(words)), 2)

    def test_speaker_change_splits_turn(self):
        words = [word("hola", 0, 300, "A"), word("qué", 400, 600, "B"), word("tal", 700, 900, "B")]
        result = turns.build_turns(words)
        self.assertEqual([t["speaker_id"] for t in result], ["A", "B"])

    def test_interjection_does_not_cut_the_main_turn(self):
        words = [word("primero", 0, 500, "A"), word("vamos", 600, 900, "A"),
                 word("vale", 1000, 1300, "B"),
                 word("y", 1500, 1600, "A"), word("después", 1700, 2200, "A")]
        result = turns.build_turns(words)
        main = [t for t in result if t["speaker_id"] == "A"]
        self.assertEqual(len(main), 1)
        self.assertEqual(main[0]["text"], "primero vamos y después")
        aside = [t for t in result if t["speaker_id"] == "B"]
        self.assertEqual(aside[0]["text"], "vale")
        self.assertTrue(aside[0]["interjection"])
        self.assertEqual(sum(t["word_count"] for t in result), 5)

    def test_monologue_is_cut_near_ninety_seconds_at_a_sentence_end(self):
        words = [word("frase." if i % 10 == 9 else "palabra", i * 1000, i * 1000 + 600, "A") for i in range(150)]
        result = turns.build_turns(words)
        self.assertGreater(len(result), 1)
        self.assertTrue(all(t["end_ms"] - t["start_ms"] <= 135_000 for t in result))
        self.assertTrue(result[0]["text"].endswith("frase."))
        self.assertEqual(sum(t["word_count"] for t in result), 150)


class VadFlagTests(unittest.TestCase):
    def test_pinned_vad_model_enables_whisper_vad(self):
        from synth.worker import asr
        if not asr.VAD_MODEL.is_file():
            self.skipTest("VAD model not downloaded (synth/scripts/download-vad-model.py)")
        flags = asr.vad_flags()
        self.assertEqual(flags[:3], ["--vad", "-vm", str(asr.VAD_MODEL)])

    def test_missing_or_tampered_vad_model_disables_vad(self):
        import tempfile
        from synth.worker import asr
        with tempfile.NamedTemporaryFile() as fake:
            fake.write(b"not the model")
            fake.flush()
            self.assertEqual(asr.vad_flags(fake.name), [])
        self.assertEqual(asr.vad_flags("/nonexistent/vad.bin"), [])

    def test_asr_model_hash_is_unchanged(self):
        from synth.worker import asr
        self.assertEqual(asr.MODEL_SHA, "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2")


if __name__ == "__main__":
    unittest.main()
