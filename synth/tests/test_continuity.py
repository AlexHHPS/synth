"""Continuity between meetings and entities feeding the glossary proposals."""
import unittest

from synth.tests.test_minutes import CONTEXT, GOOD_TOPIC_1, GOOD_TOPIC_2, TRANSCRIPT, FakeModel
from synth.worker import continuity
from synth.worker.document_context import MeetingContext, render_lines
from synth.worker.glossary_correction import mine
from synth.worker.minutes import generate_v2, render_markdown

PRIOR_CONTEXT = MeetingContext(
    title="Comité Valdivar", folder="Clientes", participants=("María",),
    open_tasks=({"key": "P1", "title": "Preparar el piloto", "owner": "Tú"},
                {"key": "P2", "title": "Pedir presupuesto a Lucerno", "owner": "Ana"}),
    open_decisions=({"key": "D1", "decision": "Usar AWS para el piloto"},))
WINDOW = render_lines(TRANSCRIPT)


class ContinuityValidationTests(unittest.TestCase):
    def test_status_needs_a_known_key_a_known_status_and_a_citation_from_this_meeting(self):
        validate = continuity.validate_continuity({"P1", "P2", "D1"}, {line["id"] for line in WINDOW})
        kept = validate({"items": [
            {"key": "D1", "status": "contradicha", "citations": ["t000002"]},
            {"key": "P2", "status": "hecha", "citations": []},             # no evidence: dropped
            {"key": "P9", "status": "hecha", "citations": ["t000001"]},    # unknown key: dropped
            {"key": "P1", "status": "cerrada", "citations": ["t000001"]},  # unknown status: dropped
            {"key": "P1", "status": "en curso", "citations": ["t999999", "t000001"]}]})
        self.assertEqual(kept, [{"key": "D1", "status": "contradicha", "citations": ["t000002"]},
                                {"key": "P1", "status": "en curso", "citations": ["t000001"]}])
        with self.assertRaises(ValueError):
            validate({"answer": "no"})

    def test_every_prior_item_gets_a_row_and_the_strongest_status_wins(self):
        prior = continuity.prior_items(PRIOR_CONTEXT)
        rows = continuity.merge_continuity(prior, [
            {"key": "P1", "status": "en curso", "citations": ["t000001"]},
            {"key": "D1", "status": "contradicha", "citations": ["t000002"]}],
            [{"previous": "P1", "lifecycle": "completada", "citations": ["t000003"]}])
        by_key = {r["key"]: r for r in rows}
        self.assertEqual((by_key["P1"]["status"], by_key["P1"]["citations"]), ("hecha", ["t000003"]))
        self.assertEqual(by_key["D1"]["status"], "contradicha")
        self.assertEqual((by_key["P2"]["status"], by_key["P2"]["citations"]), ("sin mencionar", []))
        self.assertEqual(by_key["P2"]["kind"], "tarea")


class ContinuityInMinutesTests(unittest.TestCase):
    def test_minutes_carry_the_status_of_previous_items(self):
        model = FakeModel([GOOD_TOPIC_1, GOOD_TOPIC_2],
                          continuity=[{"key": "D1", "status": "contradicha", "citations": ["t000002"]}],
                          entities=[{"name": "Valdivar", "kind": "cliente", "definition": "cliente industrial del piloto"}])
        content, markdown, _ = generate_v2(TRANSCRIPT, PRIOR_CONTEXT, model)
        by_key = {r["key"]: r for r in content["continuity"]}
        self.assertEqual(by_key["P1"]["status"], "en curso")  # the new task continues P1
        self.assertEqual(by_key["D1"]["status"], "contradicha")
        self.assertEqual(by_key["P2"]["status"], "sin mencionar")
        self.assertIn("## Pendientes de reuniones anteriores", markdown)
        self.assertIn("- Usar AWS para el piloto: contradicha [t000002]", markdown)
        self.assertEqual(content["entities"], [{"name": "Valdivar", "kind": "organizacion", "definition": "cliente industrial del piloto",
                                                "example": TRANSCRIPT["turns"][0]["text"]}])

    def test_no_previous_items_means_no_continuity_call(self):
        model = FakeModel([GOOD_TOPIC_1, GOOD_TOPIC_2])
        content, markdown, _ = generate_v2(TRANSCRIPT, MeetingContext(title="Comité"), model)
        self.assertEqual(content["continuity"], [])
        self.assertFalse([c for c in model.calls if "Pendientes de reuniones anteriores" in c[1]["content"]])
        self.assertNotIn("Pendientes de reuniones anteriores", markdown)

    def test_empty_transcript_marks_everything_unmentioned_without_a_call(self):
        model = FakeModel([])
        content, _, _ = generate_v2({**TRANSCRIPT, "turns": [], "segments": []}, PRIOR_CONTEXT, model)
        self.assertEqual({r["status"] for r in content["continuity"]}, {"sin mencionar"})
        self.assertEqual(model.calls, [])

    def test_old_minutes_without_continuity_still_render(self):
        content, _, _ = generate_v2(TRANSCRIPT, CONTEXT, FakeModel([GOOD_TOPIC_1, GOOD_TOPIC_2]))
        legacy = {k: v for k, v in content.items() if k not in ("continuity", "entities")}
        self.assertNotIn("Pendientes", render_markdown(legacy))


class EntityTests(unittest.TestCase):
    def test_only_named_defined_entities_present_in_the_transcript_are_kept(self):
        validate = continuity.validate_entities(WINDOW)
        what = "lo que se dice de él en la reunión"
        kept = validate({"entities": [
            {"name": "Valdivar", "kind": "cliente", "definition": "cliente industrial"},
            {"name": "Nimbus", "kind": "sistema", "definition": "plataforma de despliegue"},
            {"name": "Ventolia", "kind": "organizacion", "definition": what},   # not said in the meeting
            {"name": "piloto", "kind": "proyecto", "definition": what},          # common word
            {"name": "Ana", "kind": "jefa", "definition": what},                 # not a category
            {"name": "Pedro", "kind": "persona", "definition": ""},              # says nothing about what it is
            {"name": "¿Pedro?", "kind": "Persona", "definition": "responsable de la migración"}]})
        self.assertEqual([(e["name"], e["kind"], e["definition"]) for e in kept],
                         [("Valdivar", "organizacion", "cliente industrial"), ("Nimbus", "sistema", "plataforma de despliegue"),
                          ("Pedro", "persona", "responsable de la migración")])

    def test_spoken_fillers_are_never_entities(self):
        window = [{"line": "[00:01] Ana: ¿Vale? Vale, perfecto.", "text": "¿Vale? Vale, perfecto."}]
        kept = continuity.validate_entities(window)({"entities": [{"name": "Vale", "kind": "concepto", "definition": "acuerdo"},
                                                                   {"name": "¿Vale?", "kind": "concepto", "definition": "acuerdo"}]})
        self.assertEqual(kept, [])

    def test_defined_entities_are_added_directly_and_known_ones_are_not(self):
        found = mine(TRANSCRIPT, [], [], [{"name": "Nimbus", "kind": "sistema", "definition": "plataforma de despliegue",
                                           "example": "usar Nimbus"}])
        nimbus = next(p for p in found if p["term"] == "Nimbus")
        self.assertEqual((nimbus["auto"], nimbus["kind"], nimbus["definition"], nimbus["examples"]),
                         (True, "sistema", "plataforma de despliegue", ["usar Nimbus"]))
        known = mine(TRANSCRIPT, [{"term": "Nimbus", "aliases": []}], [], [{"name": "Nimbus", "kind": "sistema", "definition": "x"}])
        self.assertNotIn("Nimbus", [p["term"] for p in known])


if __name__ == "__main__":
    unittest.main()
