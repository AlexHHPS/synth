"""The acta in one pass per window: what is kept, what is dropped, and what can never fail it."""
import json
import unittest
import unittest.mock

from synth.tests.test_minutes import CONTEXT, GOOD_TOPIC_1, GOOD_TOPIC_2, TRANSCRIPT, FakeModel
from synth.worker import minutes_single
from synth.worker.minutes_single import TopicStream, draft_topic, generate_consolidated, join_topics


def run(model, *, checkpoint=None, drafts=None, saved=None, chunk=7):
    def complete(messages, tokens, on_delta=None):
        response = model(messages, tokens)
        if on_delta:
            for index in range(0, len(response["content"]), chunk):
                on_delta(response["content"][index:index + chunk])
        return response
    return generate_consolidated(TRANSCRIPT, CONTEXT, complete, checkpoint,
                                 (lambda value: saved.append(json.loads(json.dumps(value)))) if saved is not None else None,
                                 (lambda index, topic: drafts.append((index, topic))) if drafts is not None else None)


class Scripted:
    def __init__(self, *bodies):
        self.bodies, self.calls = list(bodies), []

    def __call__(self, messages, tokens):
        self.calls.append(messages)
        body = self.bodies.pop(0)
        if isinstance(body, Exception):
            raise body
        return {"content": body if isinstance(body, str) else json.dumps(body, ensure_ascii=False), "effective_model": "fake"}


class ConsolidatedMinutesTests(unittest.TestCase):
    def test_a_meeting_that_fits_one_window_is_one_call(self):
        model, drafts = FakeModel([GOOD_TOPIC_1, GOOD_TOPIC_2]), []
        content, markdown, responses = run(model, drafts=drafts)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual([t["title"] for t in content["topics"]], ["Piloto de Valdivar", "Infraestructura y permisos"])
        self.assertEqual([t["owner"] for t in content["tasks"]], ["Tú", "Ana"])
        self.assertEqual(content["decisions"][0]["decision"], "Usar Nimbus")
        self.assertEqual(content["continuity"][0]["status"], "en curso")
        self.assertIn("15 de noviembre", content["tldr"])
        # The generic point is dropped without a second call.
        self.assertEqual(len(content["topics"][0]["points"]), 1)
        # Topics were shown as they finished, in order, flagged as drafts.
        self.assertEqual([(i, t["title"], t["draft"]) for i, t in drafts],
                         [(0, "Piloto de Valdivar", True), (1, "Infraestructura y permisos", True)])
        self.assertIn("## Tareas", markdown)

    def test_invalid_items_are_dropped_and_never_fail_the_acta(self):
        topic = {**GOOD_TOPIC_2, "tasks": GOOD_TOPIC_2["tasks"] + [
            {"title": "Llamar al cliente", "owner": "Hablante 3", "citations": ["t000003"]},
            {"title": "Inventada", "owner": "Tú", "citations": ["t999999"]}]}
        content, _, _ = run(Scripted({"meeting_type": "desconocido", "topics": [{"title": "Infraestructura", **topic}],
                                      "continuity": "no es una lista", "entities": [{"name": 3}], "tldr": "Resumen."}))
        self.assertEqual([t["title"] for t in content["tasks"]], ["Revisar los permisos de las carpetas"])
        self.assertEqual(content["meeting_type"], "seguimiento")
        self.assertEqual(content["entities"], [])
        self.assertEqual(content["continuity"][0]["status"], "sin mencionar")

    def test_an_unreadable_answer_gets_one_correction(self):
        good = {"meeting_type": "comite", "topics": [{"title": "Piloto", **GOOD_TOPIC_1}], "tldr": "Resumen."}
        model = Scripted("esto no es JSON", good)
        content, _, _ = run(model)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(model.calls[1][2]["role"], "assistant")
        self.assertEqual(content["tldr"], "Resumen.")

    def test_a_missing_summary_is_built_from_the_outcomes_when_the_model_cannot_give_one(self):
        with unittest.mock.patch.object(minutes_single, "WINDOW_CHARS", 150):
            part = lambda title, topic: {"meeting_type": "comite", "topics": [{"title": title, **topic}]}
            model = Scripted(part("Piloto", GOOD_TOPIC_1), part("Piloto", {**GOOD_TOPIC_2, "tasks": []}),
                             part("Infraestructura", GOOD_TOPIC_2), part("Infraestructura", {**GOOD_TOPIC_2, "points": []}),
                             ValueError("gateway_unavailable_or_invalid"))
            saved = []
            content, _, _ = run(model, saved=saved)
        self.assertGreater(len(saved), 1)
        self.assertTrue(content["tldr"])
        # A topic continued across a cut is one topic, and nothing is listed twice.
        titles = [t["title"] for t in content["topics"]]
        self.assertEqual(len(titles), len({title.casefold() for title in titles}) if titles.count("Piloto") == 1 else -1)
        self.assertEqual(len([t for t in content["tasks"] if t["title"] == "Revisar los permisos de las carpetas"]), 1)

    def test_finished_windows_are_not_asked_again_after_a_restart(self):
        with unittest.mock.patch.object(minutes_single, "WINDOW_CHARS", 150):
            part = lambda title, topic: {"meeting_type": "comite", "topics": [{"title": title, **topic}]}
            saved = []
            first = Scripted(part("Uno", GOOD_TOPIC_1), ValueError("gateway_unavailable_or_invalid"))
            with self.assertRaises(ValueError):
                run(first, saved=saved)
            resumed = Scripted(*[part(f"Tema {i}", {**GOOD_TOPIC_2, "tasks": [], "decisions": []}) for i in range(8)],
                               {"tldr": "Resumen."})
            run(resumed, checkpoint=saved[-1])
        asked = [call[1]["content"] for call in resumed.calls]
        self.assertFalse(any("t000000" in text and "Escribe el acta" in text and "Temas ya recogidos" not in text for text in asked))

    def test_an_answer_too_long_for_the_output_is_read_in_two_halves(self):
        transcript = {**TRANSCRIPT, "turns": [{**TRANSCRIPT["turns"][i % 4], "id": f"t{i:06d}"} for i in range(24)]}
        half = {"meeting_type": "comite", "topics": [{"title": "Mitad", "context": "", "discussion": "Se habla.", "outcome": "",
                                                      "points": [], "tasks": [], "decisions": [], "questions": []}]}
        model = Scripted(ValueError("gateway_output_incomplete"), half, {**half, "topics": [{**half["topics"][0], "title": "Otra"}]},
                         {"tldr": "Resumen."})
        content, _, _ = generate_consolidated(transcript, CONTEXT, lambda m, t, d=None: model(m, t))
        self.assertEqual([t["title"] for t in content["topics"]], ["Mitad", "Otra"])

    def test_an_empty_transcript_needs_no_model(self):
        content, _, responses = generate_consolidated({**TRANSCRIPT, "turns": [], "segments": []}, CONTEXT, None)
        self.assertEqual((content["topics"], responses), ([], []))


class TopicStreamTests(unittest.TestCase):
    def test_topics_are_found_as_soon_as_each_one_closes(self):
        text = json.dumps({"meeting_type": "comite", "topics": [
            {"title": "Uno {con} llaves \"y\" comillas ]", "points": [{"text": "a", "citations": ["t1"]}]},
            {"title": "Dos", "points": []}], "tldr": "fin"}, ensure_ascii=False)
        stream, found = TopicStream(), []
        for char in text:
            found += [(len(found), topic["title"]) for topic in stream.feed(char)]
        self.assertEqual([title for _, title in found], ["Uno {con} llaves \"y\" comillas ]", "Dos"])
        self.assertEqual(stream.feed(', "topics": [{"title": "Tres"}]'), [])

    def test_a_draft_shows_text_only_with_real_citations(self):
        draft = draft_topic({"title": " Uno ", "context": 3, "points": [{"text": "p", "citations": ["t1", "zz"]}, "x"],
                             "tasks": [{"title": "no validada"}]}, {"t1"})
        self.assertEqual((draft["title"], draft["context"], draft["points"], draft["tasks"]),
                         ("Uno", "", [{"text": "p", "citations": ["t1"]}], []))
        self.assertIsNone(draft_topic({"points": []}, set()))

    def test_join_keeps_order_and_removes_repeats(self):
        topic = lambda title, task: {"title": title, "context": "", "discussion": "", "outcome": "", "points": [],
                                     "tasks": [{"title": task}], "decisions": [], "questions": []}
        joined = join_topics([{"topics": [topic("A", "x")]}, {"topics": [topic("a", "y"), topic("B", "X")]}])
        self.assertEqual([(t["title"], [k["title"] for k in t["tasks"]]) for t in joined], [("A", ["x", "y"]), ("B", [])])


if __name__ == "__main__":
    unittest.main()


class GatewayLimitTests(unittest.TestCase):
    def test_a_request_above_the_maximum_is_served_at_the_maximum_not_refused(self):
        # 0.6.0 shipped asking 12288 tokens; Hermes refused every acta with 422 before any model ran.
        from synth.hermes.app import MAX_TOKENS, Completion
        from synth.worker import omniroute
        body = Completion(model="m", messages=[{"role": "user", "content": "x"}], max_tokens=50_000)
        self.assertEqual(body.max_tokens, MAX_TOKENS)
        self.assertEqual(omniroute.MAX_OUTPUT_TOKENS, MAX_TOKENS)
        self.assertLessEqual(minutes_single.MAX_TOKENS, MAX_TOKENS)
