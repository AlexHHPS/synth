import unittest

from synth.eval import metrics


def seg(text, start, end, source="system", speaker=None, sid=None):
    return {"id": sid or f"s{start}", "text": text, "start_ms": start, "end_ms": end,
            "source_id": source, "speaker_id": speaker}


class TurnMetricsTests(unittest.TestCase):
    def test_turns_per_minute_and_median_words(self):
        turns = [seg("uno dos tres", 0, 1000), seg("uno dos tres cuatro cinco", 1000, 2000),
                 seg("uno", 2000, 3000)]
        stats = metrics.turn_stats(turns, duration_ms=120_000)
        self.assertEqual(stats["turns_per_min"], 1.5)
        self.assertEqual(stats["median_words_per_turn"], 3)

    def test_empty_transcript_has_zero_turns(self):
        self.assertEqual(metrics.turn_stats([], duration_ms=60_000),
                         {"turns_per_min": 0.0, "median_words_per_turn": 0})


class DuplicateTests(unittest.TestCase):
    def test_microphone_echo_of_system_counts_as_duplicate(self):
        segments = [seg("quieres que preparemos la apertura de una ronda", 0, 4000, "system"),
                    seg("quieres que preparemos la apertura", 100, 3000, "microphone")]
        self.assertGreater(metrics.cross_channel_duplicate_pct(segments), 30)

    def test_distinct_speech_is_not_duplicate(self):
        segments = [seg("quieres que preparemos la apertura de una ronda", 0, 4000, "system"),
                    seg("yo prefiero revisar el presupuesto primero", 100, 3000, "microphone")]
        self.assertEqual(metrics.cross_channel_duplicate_pct(segments), 0.0)

    def test_same_text_far_in_time_is_not_duplicate(self):
        segments = [seg("quieres que preparemos la apertura de una ronda", 0, 4000, "system"),
                    seg("quieres que preparemos la apertura", 60_000, 63_000, "microphone")]
        self.assertEqual(metrics.cross_channel_duplicate_pct(segments), 0.0)


class SpeakerTests(unittest.TestCase):
    def test_speaker_count_ignores_unlabelled(self):
        segments = [seg("a", 0, 1, speaker="system:S1"), seg("b", 1, 2, speaker="system:S2"),
                    seg("c", 2, 3, speaker=None), seg("d", 3, 4, speaker="system:S1")]
        self.assertEqual(metrics.speaker_count(segments), 2)

    def test_speaker_count_within_tolerance(self):
        self.assertTrue(metrics.speaker_count_ok(4, 3))
        self.assertFalse(metrics.speaker_count_ok(9, 3))


class IdentityTests(unittest.TestCase):
    reference = {"m1": {"operator_seconds": 40, "operator_clusters": ["microphone:S1"]},
                 "m2": {"operator_seconds": 30, "operator_clusters": ["system:S2"]},
                 "m3": {"operator_seconds": 2, "operator_clusters": ["microphone:S1"]}}

    def test_coverage_counts_only_meetings_where_operator_speaks_enough(self):
        suggestions = [{"meeting": "m1", "cluster": "microphone:S1", "level": "high"}]
        scores = metrics.identity_scores(suggestions, self.reference)
        self.assertEqual(scores["eligible_meetings"], 2)
        self.assertEqual(scores["coverage"], 0.5)
        self.assertEqual(scores["wrong_high"], 0)

    def test_wrong_high_suggestion_is_counted(self):
        suggestions = [{"meeting": "m1", "cluster": "microphone:S1", "level": "high"},
                       {"meeting": "m2", "cluster": "system:S9", "level": "high"}]
        scores = metrics.identity_scores(suggestions, self.reference)
        self.assertEqual(scores["wrong_high"], 1)
        self.assertEqual(scores["precision"], 0.5)


class GlossaryTests(unittest.TestCase):
    def test_recall_of_approved_terms(self):
        reference = "Valdivar y Lucerno firmaron con Valdivar"
        corrected = "Valdivar y Luservo firmaron con Valdivar"
        self.assertAlmostEqual(metrics.glossary_recall(corrected, reference, ["Valdivar", "Lucerno"]), 2 / 3, places=3)

    def test_false_substitution_rate(self):
        corrections = [{"segment_id": "a", "to": "Valdivar"}, {"segment_id": "b", "to": "Lucerno"}]
        reference = {"a": "habló con Valdivar", "b": "el consumo creció"}
        self.assertEqual(metrics.false_substitution_rate(corrections, reference), 0.5)
        self.assertEqual(metrics.false_substitution_rate([], reference), 0.0)


class DocumentTests(unittest.TestCase):
    def test_generic_points_are_detected(self):
        points = ["Se discutieron varios temas importantes.",
                  "Valdivar pide el piloto antes del 15 de noviembre.",
                  "El equipo habló de la situación general del proyecto."]
        self.assertAlmostEqual(metrics.generic_point_pct(points), 200 / 3, places=3)

    def test_specific_point_with_number_is_not_generic(self):
        self.assertEqual(metrics.generic_point_pct(["El presupuesto sube a 40.000 euros."]), 0.0)

    def test_key_fact_recall_uses_keyword_groups(self):
        facts = [{"id": "f1", "keywords": ["postgres", "nimbus"]},
                 {"id": "f2", "keywords": ["cloudflare|waf"]},
                 {"id": "f3", "keywords": ["multi-tenant|multitenant", "bucket"]}]
        text = "Se usará Postgres en Nimbus y delante irá Cloudflare."
        self.assertAlmostEqual(metrics.key_fact_recall(text, facts), 2 / 3, places=3)

    def test_citation_fidelity(self):
        segments = {"s1": "vamos a usar postgres como servicio en nimbus",
                    "s2": "hay que montar el bucket"}
        items = [{"text": "Usar Postgres en Nimbus", "citations": ["s1"]},
                 {"text": "Montar el bucket", "citations": ["s1"]},
                 {"text": "Contratar a Ana", "citations": []}]
        self.assertAlmostEqual(metrics.citation_fidelity(items, segments), 1 / 3, places=3)

    def test_tasks_with_owner_or_justified_unassigned(self):
        tasks = [{"owner": "Alex"}, {"owner": None}, {"owner": "sin asignar", "owner_evidence": "nadie se comprometió"},
                 {"owner": "sin asignar"}]
        self.assertEqual(metrics.tasks_with_owner_pct(tasks), 50.0)

    def test_decisions_with_reason(self):
        decisions = [{"reason": "porque el cliente exige datos en la UE"}, {"reason": ""}, {}]
        self.assertAlmostEqual(metrics.decisions_with_reason_pct(decisions), 100 / 3, places=3)


class DocumentAdapterTests(unittest.TestCase):
    def test_v1_document_items_are_extracted(self):
        document = {"content": {"summary": [{"text": "Punto", "evidence": [{"segment_id": "s1", "quote": "x"}]}],
                                "decisions": [{"text": "Dec", "evidence": [{"segment_id": "s2", "quote": "y"}],
                                               "budget_eur": None, "pilot_date": None}],
                                "actions": [{"text": "Tarea", "evidence": [], "owner": "Alex", "due_date": None}],
                                "open_questions": []}}
        parts = metrics.document_parts(document)
        self.assertEqual(parts["points"][0]["citations"], ["s1"])
        self.assertEqual(parts["tasks"][0]["owner"], "Alex")
        self.assertEqual(parts["decisions"][0].get("reason"), None)
        self.assertIn("Punto", parts["text"])


if __name__ == "__main__":
    unittest.main()
