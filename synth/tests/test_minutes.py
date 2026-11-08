import datetime
import json
import unittest

from synth.worker import minutes
from synth.worker.document_context import MeetingContext, render_lines, windows

TRANSCRIPT = {
    "language": "es", "model_fingerprint": "whisper:test", "duration_ms": 120000,
    "segments": [{"id": f"s{i:06d}", "text": "x", "start_ms": i * 1000, "end_ms": i * 1000 + 500, "speaker_id": "system:S1"}
                 for i in range(4)],
    "turns": [
        {"id": "t000000", "text": "Soy Pedro y propongo pasar el piloto de Valdivar al 15 de noviembre.", "start_ms": 0,
         "end_ms": 5000, "speaker_id": "system:S1", "segment_ids": ["s000000"], "word_count": 13},
        {"id": "t000001", "text": "Vale, lo preparo yo con presupuesto de 40.000 euros.", "start_ms": 6000,
         "end_ms": 9000, "speaker_id": "microphone:local", "segment_ids": ["s000001"], "word_count": 9},
        {"id": "t000002", "text": "Decidimos usar Nimbus porque el cliente quiere datos en la UE.", "start_ms": 10000,
         "end_ms": 14000, "speaker_id": "system:S2", "segment_ids": ["s000002"], "word_count": 11},
        {"id": "t000003", "text": "Ana revisará los permisos de las carpetas.", "start_ms": 15000,
         "end_ms": 18000, "speaker_id": "system:S2", "segment_ids": ["s000003"], "word_count": 7}],
    "speakers": {"system:S2": {"status": "confirmed", "name": "María", "employee_id": None}},
}
CONTEXT = MeetingContext(title="Comité Valdivar", started_at=datetime.datetime(2026, 10, 27, 10, 0), folder="Clientes",
                         participants=("María",), glossary=({"term": "Valdivar", "definition": "cliente industrial"},),
                         open_tasks=({"key": "P1", "title": "Preparar el piloto", "owner": "Tú"},))


class FakeModel:
    """Scripted answers keyed by what the prompt asks for; records every call."""

    def __init__(self, topic_answers, continuity=(), entities=()):
        self.calls, self.topic_answers = [], list(topic_answers)
        self.continuity, self.entities = list(continuity), list(entities)

    def __call__(self, messages, max_tokens):
        self.calls.append(messages)
        user = messages[1]["content"]
        if "Divide este tramo" in user:
            body = {"meeting_type": "comite", "topics": [{"title": "Piloto de Valdivar", "first": "t000000", "last": "t000001"},
                                                          {"title": "Infraestructura y permisos", "first": "t000002", "last": "t000003"}]}
        elif "Revisa las líneas y devuelve SOLO lo que falte" in user:
            body = {"points": [], "tasks": [], "decisions": [], "questions": []}
        elif "Estos puntos son genéricos" in user:
            body = {"points": [{"text": None, "citations": ["t000000"]}]}
        elif "Pendientes de reuniones anteriores" in user:
            body = {"items": self.continuity}
        elif "Lista los nombres propios relevantes" in user:
            body = {"entities": self.entities}
        elif "Resume la reunión" in user:
            body = {"tldr": "El comité mueve el piloto de Valdivar al 15 de noviembre y elige Nimbus por la UE."}
        else:
            body = self.topic_answers.pop(0) if len(messages) == 2 else self.retry_answer(messages)
        return {"content": json.dumps(body, ensure_ascii=False), "effective_model": "fake"}

    def retry_answer(self, messages):
        return self.topic_answers.pop(0)


GOOD_TOPIC_1 = {"context": "Pedro abre el comité.", "discussion": "Se propone mover el piloto.", "outcome": "El piloto pasa al 15 de noviembre.",
                "points": [{"text": "El piloto de Valdivar pasa al 15 de noviembre.", "citations": ["t000000"]},
                           {"text": "Se habló de la situación general del proyecto.", "citations": ["t000000"]}],
                "tasks": [{"title": "Preparar el piloto con 40.000 euros", "owner": "Tú", "owner_evidence": "autocompromiso",
                           "due_text": None, "due_date": None, "lifecycle": "continuacion", "previous": "P1", "citations": ["t000001"]}],
                "decisions": [], "questions": []}
GOOD_TOPIC_2 = {"context": "", "discussion": "", "outcome": "Nimbus por la UE.",
                "points": [{"text": "Se usará Nimbus porque el cliente exige datos en la UE.", "citations": ["t000002"]}],
                "tasks": [{"title": "Revisar los permisos de las carpetas", "owner": "Ana", "owner_evidence": "explicito",
                           "citations": ["t000003"]}],
                "decisions": [{"decision": "Usar Nimbus", "reason": "el cliente quiere datos en la UE", "status": "acordada",
                               "citations": ["t000002"]}], "questions": []}


class ContextTests(unittest.TestCase):
    def test_header_has_weekday_participants_glossary_and_open_tasks(self):
        header = CONTEXT.header()
        self.assertIn("martes 27 de octubre de 2026", header)
        self.assertIn("María", header)
        self.assertIn("Valdivar (cliente industrial)", header)
        self.assertIn("[P1] Preparar el piloto", header)

    def test_turn_lines_use_display_names_not_json_or_ids(self):
        lines = [line["line"] for line in render_lines(TRANSCRIPT)]
        self.assertEqual(lines[0], "[t000000 00:00] Hablante 1: Soy Pedro y propongo pasar el piloto de Valdivar al 15 de noviembre.")
        self.assertTrue(lines[1].startswith("[t000001 00:06] Tú:"))
        self.assertTrue(lines[2].startswith("[t000002 00:10] María:"))
        self.assertNotIn("system:S1", "\n".join(lines))

    def test_long_meetings_are_split_into_windows(self):
        lines = [{"id": f"t{i:06d}", "line": "x" * 1000} for i in range(60)]
        self.assertGreater(len(windows(lines, maximum=24_000)), 1)


class PipelineTests(unittest.TestCase):
    def run_pipeline(self, answers):
        model = FakeModel(answers)
        content, markdown, responses = minutes.generate_v2(TRANSCRIPT, CONTEXT, model)
        return content, markdown, model

    def test_full_pipeline_produces_schema_v2(self):
        content, markdown, _ = self.run_pipeline([GOOD_TOPIC_1, GOOD_TOPIC_2])
        self.assertEqual(content["schema_version"], 2)
        self.assertEqual(content["meeting_type"], "comite")
        self.assertEqual([t["title"] for t in content["topics"]], ["Piloto de Valdivar", "Infraestructura y permisos"])
        self.assertEqual(content["tasks"][0]["owner"], "Tú")
        self.assertEqual(content["tasks"][0]["lifecycle"], "continuacion")
        self.assertEqual(content["decisions"][0]["reason"], "el cliente quiere datos en la UE")
        self.assertIn("## Tareas", markdown)

    def test_generic_point_is_rewritten_or_dropped(self):
        content, _, _ = self.run_pipeline([GOOD_TOPIC_1, GOOD_TOPIC_2])
        points = [p["text"] for t in content["topics"] for p in t["points"]]
        self.assertNotIn("Se habló de la situación general del proyecto.", points)
        self.assertTrue(all(not minutes.is_generic(p, ["Valdivar", "María"]) for p in points))

    def test_unsupported_owner_is_never_published(self):
        invented = json.loads(json.dumps(GOOD_TOPIC_2))
        invented["tasks"][0]["owner"] = "Pedro García"
        content, _, model = self.run_pipeline([GOOD_TOPIC_1, invented, invented])
        owners = [t["owner"] for t in content["tasks"]]
        self.assertNotIn("Pedro García", owners)
        retry = [m for m in model.calls if len(m) == 4]
        self.assertTrue(retry and retry[0][2]["role"] == "assistant")

    def test_unconfirmed_speaker_is_never_named_as_owner(self):
        named = json.loads(json.dumps(GOOD_TOPIC_1))
        named["tasks"][0]["owner"] = "Hablante 1"
        content, _, _ = self.run_pipeline([named, named, GOOD_TOPIC_2])
        self.assertNotIn("Hablante 1", [t["owner"] for t in content["tasks"]])

    def test_citation_outside_the_topic_is_rejected(self):
        bad = json.loads(json.dumps(GOOD_TOPIC_1))
        bad["points"][0]["citations"] = ["t000003"]
        content, _, _ = self.run_pipeline([bad, bad, GOOD_TOPIC_2])
        cited = [c for p in content["topics"][0]["points"] for c in p["citations"]]
        self.assertNotIn("t000003", cited)

    def test_each_call_is_small_and_bounded(self):
        _, _, model = self.run_pipeline([GOOD_TOPIC_1, GOOD_TOPIC_2])
        self.assertTrue(all(len(m[1]["content"]) < 30_000 for m in model.calls))


class SchemaTests(unittest.TestCase):
    def test_outline_must_cover_lines_in_order(self):
        window = [{"id": "t000000", "line": "a"}, {"id": "t000001", "line": "b"}]
        check = minutes.validate_outline(window, True)
        with self.assertRaises(ValueError):
            check({"meeting_type": "comite", "topics": [{"title": "B", "first": "t000001", "last": "t000001"},
                                                        {"title": "A", "first": "t000000", "last": "t000000"}]})
        with self.assertRaises(ValueError):
            check({"meeting_type": "reunion", "topics": [{"title": "A", "first": "t000000", "last": "t000001"}]})



class ClaimEffectTests(unittest.TestCase):
    """a transcript that tempts the model to invent a person, a date or a commitment."""

    def validator(self):
        topic = {"title": "Piloto", "lines": render_lines(TRANSCRIPT)[:2]}
        return minutes.validate_topic(topic, {"María", "Tú"}, set(), started=datetime.datetime(2026, 10, 27))

    def task(self, **fields):
        base = {"title": "Preparar el piloto", "owner": "Tú", "owner_evidence": "autocompromiso", "citations": ["t000001"]}
        return {"points": [], "tasks": [{**base, **fields}], "decisions": [], "questions": []}

    def test_invented_date_is_rejected(self):
        with self.assertRaises(ValueError):
            self.validator()(self.task(due_text="el viernes", due_date="2026-10-30"))
        with self.assertRaises(ValueError):
            self.validator()(self.task(due_date="2026-10-30"))

    def test_date_said_in_the_citation_is_kept(self):
        result = self.validator()(self.task(due_text="15 de noviembre", due_date="2026-11-15", citations=["t000000"],
                                            owner="sin asignar", owner_evidence="ninguno"))
        self.assertEqual(result["tasks"][0]["due_date"], "2026-11-15")

    def test_invented_person_and_decider_are_rejected(self):
        with self.assertRaises(ValueError):
            self.validator()(self.task(owner="Carlos"))
        decision = {"points": [], "tasks": [], "questions": [],
                    "decisions": [{"decision": "Mover el piloto", "decided_by": "Carlos", "citations": ["t000000"]}]}
        with self.assertRaises(ValueError):
            self.validator()(decision)

    def test_person_named_in_the_citation_is_allowed(self):
        decision = {"points": [], "tasks": [], "questions": [],
                    "decisions": [{"decision": "Mover el piloto", "status":"acordada", "decided_by": "Pedro", "citations": ["t000000"]}]}
        self.assertEqual(self.validator()(decision)["decisions"][0]["decided_by"], "Pedro")


class UncertainASRTests(unittest.TestCase):
    def topic(self):
        transcript = json.loads(json.dumps(TRANSCRIPT))
        transcript['segments'][2]['evidence_state'] = 'needs_review'
        transcript['asr_evidence'] = [{'raw_text': 'Una alternativa que no debe añadirse a lo dicho'}]
        return {'title': 'Piloto', 'lines': render_lines(transcript)}

    def test_uncertainty_reaches_the_cited_turn_but_raw_alternatives_are_not_spoken_lines(self):
        lines = self.topic()['lines']
        self.assertEqual(lines[2]['evidence_state'], 'needs_review')
        self.assertIn('ASR pendiente de revisión', lines[2]['line'])
        self.assertNotIn('evidence_state', lines[1])
        self.assertNotIn('Una alternativa', '\n'.join(line['line'] for line in lines))

    def test_uncertain_citation_cannot_confirm_a_decision_even_with_a_stable_extra_citation(self):
        check = minutes.validate_topic(self.topic(), {'María'}, set())
        decision = {'decision': 'Usar Nimbus', 'status': 'acordada', 'citations': ['t000001', 't000002']}
        with self.assertRaisesRegex(ValueError, 'decision_con_asr_pendiente_de_revision'):
            check({'decisions': [decision]})
        decision['status'] = 'pendiente de validar'
        result = check({'decisions': [decision]})
        self.assertEqual(result['decisions'][0]['status'], 'pendiente de validar')

    def test_uncertain_citation_does_not_confirm_an_owner_date_or_completion(self):
        check = minutes.validate_topic(self.topic(), {'María'}, {'P1'})
        base = {'title': 'Revisar infraestructura', 'citations': ['t000002'], 'owner': 'sin asignar'}
        for extra in [{'owner': 'María'}, {'lifecycle': 'completada', 'previous': 'P1'}]:
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, 'compromiso_con_asr_pendiente_de_revision'):
                check({'tasks': [{**base, **extra}]})
        self.assertEqual(check({'tasks': [base]})['tasks'][0]['owner'], 'sin asignar')



class TemplateTests(unittest.TestCase):
    def test_six_meeting_types_have_templates_and_reach_the_prompt(self):
        for kind in ("seguimiento", "comite", "1:1", "cliente", "kickoff", "brainstorming"):
            with self.subTest(kind=kind):
                prompt = minutes.topic_prompt(CONTEXT, kind, {"title": "Tema", "lines": render_lines(TRANSCRIPT)[:1]})
                self.assertIn(minutes.TEMPLATES[kind], prompt)
                self.assertIn(f"Tipo de reunión: {kind}", prompt)

    def test_old_v1_minutes_are_still_readable(self):
        from synth.eval.metrics import document_parts
        v1 = {"content": {"summary": [{"text": "Punto", "evidence": [{"segment_id": "s000001", "quote": "q"}]}],
                          "decisions": [], "actions": [], "open_questions": []}}
        v2 = {"content": minutes.assemble("comite", "TL;DR", [{"title": "A", "context": "", "discussion": "", "outcome": "",
                                                              "points": [{"text": "Punto 2", "citations": ["t000001"]}],
                                                              "tasks": [], "decisions": [], "questions": []}])}
        self.assertEqual(document_parts(v1)["points"][0]["citations"], ["s000001"])
        self.assertEqual(document_parts(v2)["points"][0]["citations"], ["t000001"])

class ReviewW4Tests(unittest.TestCase):
    lines = render_lines(TRANSCRIPT)

    def test_confirmed_participant_needs_support_in_the_citation(self):
        cited = [self.lines[0]]  # Hablante 1 speaking; María is not named
        self.assertIsNone(minutes.owner_grounding("María", cited))
        self.assertEqual(minutes.owner_grounding("María", [self.lines[2]]), "autocompromiso")
        self.assertEqual(minutes.owner_grounding("Pedro", cited), "explicito")

    def test_name_inside_another_word_is_not_a_mention(self):
        line = {"speaker": "Hablante 1", "text": "Lo vemos la semana que viene."}
        self.assertIsNone(minutes.owner_grounding("Ana", [line]))

    def test_due_dates_are_resolved_from_the_meeting_date_not_the_model(self):
        tuesday = datetime.datetime(2026, 10, 27)
        self.assertEqual(minutes.resolve_due("el viernes", "2030-01-01", tuesday), "2026-10-30")
        self.assertEqual(minutes.resolve_due("mañana", None, tuesday), "2026-10-28")
        self.assertEqual(minutes.resolve_due("antes del 15 de noviembre", "2026-12-01", tuesday), "2026-11-15")
        self.assertIsNone(minutes.resolve_due("cuando se pueda", "2026-11-01", tuesday))

    def test_empty_transcript_makes_no_model_call(self):
        calls = []
        empty = {"segments": [], "turns": []}
        content, _, _ = minutes.generate_v2(empty, CONTEXT, lambda m, t: calls.append(m))
        self.assertEqual(calls, [])
        self.assertEqual(content["tldr"], "Sin contenido transcrito.")

    def test_outline_gaps_are_covered(self):
        window = [{"id": f"t{i:06d}", "line": "x"} for i in range(6)]
        outline = minutes.validate_outline(window, False)({"topics": [{"title": "A", "first": "t000001", "last": "t000002"},
                                                                      {"title": "B", "first": "t000004", "last": "t000004"}]})
        covered = [line["id"] for topic in outline["topics"] for line in topic["lines"]]
        self.assertEqual(covered, [f"t{i:06d}" for i in range(6)])

class MalformedAnswerTests(unittest.TestCase):
    def test_wrong_shape_gets_the_correction_turn_instead_of_crashing(self):
        broken = {**GOOD_TOPIC_1, "tasks": ["Preparar el piloto"], "points": "una frase suelta"}
        model = FakeModel([broken, GOOD_TOPIC_1, GOOD_TOPIC_2])
        content, _, _ = minutes.generate_v2(TRANSCRIPT, CONTEXT, model)
        retries = [c for c in model.calls if len(c) == 4]
        self.assertTrue(retries)
        self.assertIn("esquema_invalido", retries[0][3]["content"])
        self.assertEqual(content["tasks"][0]["title"], "Preparar el piloto con 40.000 euros")

    def test_wrong_shape_twice_keeps_the_valid_items_and_drops_the_malformed_ones(self):
        broken = {**GOOD_TOPIC_1, "tasks": ["x"]}
        content, _, _ = minutes.generate_v2(TRANSCRIPT, CONTEXT, FakeModel([broken, broken, GOOD_TOPIC_2]))
        self.assertNotIn("x", [t["title"] for t in content["tasks"]])
        self.assertIn("El piloto de Valdivar pasa al 15 de noviembre.", [p["text"] for t in content["topics"] for p in t["points"]])


class UnifiedReaderTests(unittest.TestCase):
    def test_v1_minutes_are_served_in_the_v2_shape(self):
        from synth.server.minutes_view import as_v2
        v1 = {"language": "es", "summary": [{"text": "Se usará Nimbus.", "evidence": [{"segment_id": "s000002", "quote": "q"}]}],
              "decisions": [{"text": "Usar Nimbus", "evidence": [{"segment_id": "s000002", "quote": "q"}], "budget_eur": None, "pilot_date": None}],
              "actions": [{"text": "Montar el bucket", "evidence": [{"segment_id": "s000003", "quote": "q"}], "owner": "Alex", "due_date": None}],
              "open_questions": []}
        view = as_v2(v1)
        self.assertEqual((view["schema_version"], view["converted_from"]), (2, 1))
        broken = as_v2({"summary": [{"text": "Punto", "evidence": [{"quote": "sin id"}, "s000004"]}]})
        self.assertEqual(broken["topics"][0]["points"][0]["citations"], ["s000004"])
        self.assertEqual(view["topics"][0]["points"][0]["citations"], ["s000002"])
        self.assertEqual(view["tasks"][0]["owner"], "Alex")
        self.assertEqual(view["decisions"][0]["decision"], "Usar Nimbus")
        v2 = minutes.assemble("comite", "T", [])
        self.assertIs(as_v2(v2), v2)

    def test_minutes_endpoint_requires_a_session(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        from uuid import uuid4
        from fastapi.testclient import TestClient
        from synth.server import app as api

        @contextmanager
        def fake_connect():
            yield object()
        with patch("synth.server.app.connect", fake_connect):
            self.assertEqual(TestClient(api.app).get(f"/v1/meetings/{uuid4()}/minutes").status_code, 401)


if __name__ == "__main__":
    unittest.main()
