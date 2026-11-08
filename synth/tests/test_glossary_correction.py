import copy
import json
import unittest
import unittest.mock

from synth.worker import glossary_correction as gc
from synth.worker.phonetic import candidates, similarity

TERMS = [{"id": "g1", "term": "Valdivar", "aliases": []}, {"id": "g2", "term": "Lucerno", "aliases": []},
         {"id": "g3", "term": "Nimbus", "aliases": ["Nimbul"]}]


def transcript(*texts):
    return {"language": "es", "model_fingerprint": "whisper:x", "duration_ms": 60000,
            "segments": [{"id": f"s{i:06d}", "text": t, "start_ms": i * 1000, "end_ms": i * 1000 + 900} for i, t in enumerate(texts)],
            "turns": [{"id": f"t{i:06d}", "text": t, "start_ms": i * 1000, "end_ms": i * 1000 + 900, "segment_ids": [f"s{i:06d}"],
                       "word_count": len(t.split())} for i, t in enumerate(texts)]}


class Model:
    """Accepts or rejects every candidate; can try to inject unknown ids."""

    def __init__(self, apply=True, extra_ids=(), doubtful=()):
        self.apply, self.extra_ids, self.doubtful, self.calls = apply, extra_ids, doubtful, []

    def __call__(self, messages, max_tokens):
        self.calls.append(messages)
        ids = [line.split(":")[0] for line in messages[1]["content"].split("\n") if line.startswith("k")]
        decisions = [{"id": i, "apply": self.apply} for i in ids + list(self.extra_ids)]
        return {"content": json.dumps({"decisions": decisions, "doubtful": list(self.doubtful)}), "effective_model": "fake"}


class PhoneticTests(unittest.TestCase):
    def test_baldibar_and_luservo_sound_like_the_terms(self):
        self.assertGreaterEqual(similarity("Baldibar", "Valdivar"), 0.9)
        self.assertGreaterEqual(similarity("Luservo", "Lucerno"), 0.9)

    def test_common_lowercase_words_are_never_candidates(self):
        text = "el consumo del cuerpo técnico en el barrio del gobierno"
        self.assertEqual(candidates(text, TERMS), [])


class CorrectionTests(unittest.TestCase):
    def test_baldibar_to_valdivar_and_luservo_to_lucerno_as_a_new_version(self):
        raw = transcript("Hablamos con Baldibar sobre el piloto.", "Y luego con Luservo, que pide fecha.")
        before = copy.deepcopy(raw)
        corrected, _, _ = gc.correct(raw, TERMS, Model(), raw_version=1)
        self.assertEqual(raw, before)  # the raw ASR version is never modified
        self.assertEqual([s["text"] for s in corrected["segments"]],
                         ["Hablamos con Valdivar sobre el piloto.", "Y luego con Lucerno, que pide fecha."])
        self.assertEqual(corrected["turns"][0]["text"], "Hablamos con Valdivar sobre el piloto.")
        self.assertEqual([(c["original"], c["replacement"]) for c in corrected["corrections"]],
                         [("Baldibar", "Valdivar"), ("Luservo", "Lucerno")])
        self.assertEqual(corrected["raw_version"], 1)

    def test_known_alias_is_corrected(self):
        corrected, _, _ = gc.correct(transcript("Lo desplegamos en Nimbul mañana."), TERMS, Model(), 1)
        self.assertEqual(corrected["segments"][0]["text"], "Lo desplegamos en Nimbus mañana.")

    def test_model_rejection_changes_nothing(self):
        corrected, _, _ = gc.correct(transcript("Hablamos con Baldibar."), TERMS, Model(apply=False), 1)
        self.assertIsNone(corrected)

    def test_an_unreadable_answer_gets_one_correction_turn(self):
        class Prose(Model):
            def __call__(self, messages, max_tokens):
                if len(messages) == 2:
                    self.calls.append(messages)
                    return {"content": "Claro, aquí tienes: acepto k1."}
                return super().__call__(messages, max_tokens)
        model = Prose()
        with self.assertLogs("synth.worker", "INFO") as logs:
            corrected, _, _ = gc.correct(transcript("Hablamos con Baldibar."), TERMS, model, 1)
        self.assertEqual(corrected["segments"][0]["text"], "Hablamos con Valdivar.")
        self.assertEqual(len(model.calls), 2)
        self.assertIn("glossary_verify candidates=1 accepted=1 unreadable_batches=0", logs.output[-1])

    def test_an_answer_unreadable_twice_accepts_nothing_and_says_so(self):
        class Garbage(Model):
            def __call__(self, messages, max_tokens):
                self.calls.append(messages)
                return {"content": "no"}
        with self.assertLogs("synth.worker", "INFO") as logs:
            corrected, _, _ = gc.correct(transcript("Hablamos con Baldibar."), TERMS, Garbage(), 1)
        self.assertIsNone(corrected)
        self.assertIn("unreadable_batches=1", logs.output[-1])

    def test_a_doubtful_name_guessed_as_the_candidate_term_is_accepted(self):
        model = Model(apply=False, doubtful=[{"heard": "Baldibar", "guess": "Valdivar"}, {"heard": "Atlantia", "guess": "Atlántica"}])
        corrected, doubtful, _ = gc.correct(transcript("Hablamos con Baldibar y Atlantia."), TERMS, model, 1)
        self.assertEqual(corrected["segments"][0]["text"], "Hablamos con Valdivar y Atlantia.")
        self.assertEqual(doubtful, [{"heard": "Atlantia", "guess": "Atlántica"}])

    def test_the_model_is_told_what_each_term_is(self):
        model = Model()
        terms = [{**TERMS[0], "kind": "organizacion", "definition": "cliente industrial"}, *TERMS[1:]]
        gc.correct(transcript("Hablamos con Baldibar."), terms, model, 1)
        self.assertIn("«Valdivar» (organización: cliente industrial)", model.calls[0][1]["content"])

    def test_model_cannot_add_its_own_ids(self):
        corrected, _, _ = gc.correct(transcript("El consumo sube y hablamos con Baldibar."), TERMS, Model(extra_ids=["k99", "s000000"]), 1)
        self.assertEqual(corrected["segments"][0]["text"], "El consumo sube y hablamos con Valdivar.")
        self.assertEqual(len(corrected["corrections"]), 1)

    def test_common_words_are_not_substituted_even_if_the_model_says_yes(self):
        corrected, _, _ = gc.correct(transcript("el consumo y el cuerpo técnico"), TERMS, Model(), 1)
        self.assertIsNone(corrected)

    def test_already_corrected_versions_are_not_corrected_again(self):
        corrected, _, _ = gc.correct(transcript("Hablamos con Baldibar."), TERMS, Model(), 1)
        self.assertIsNone(gc.correct(corrected, TERMS, Model(), 2)[0])

    def test_undo_restores_the_heard_text(self):
        corrected, _, _ = gc.correct(transcript("Hablamos con Baldibar."), TERMS, Model(), 1)
        undone = gc.undo(corrected, "c0001")
        self.assertEqual(undone["segments"][0]["text"], "Hablamos con Baldibar.")
        self.assertEqual(undone["corrections"][0]["status"], "undone")
        with self.assertRaises(ValueError):
            gc.undo(undone, "c0001")


class PositionTests(unittest.TestCase):
    """Undo and turn edits act on the corrected occurrence, never on another spelling of the same word."""

    def test_undo_keeps_a_correctly_spelled_earlier_occurrence(self):
        corrected, _, _ = gc.correct(transcript("Valdivar firmó y Baldibar pagó."), TERMS, Model(), 1)
        self.assertEqual(corrected["segments"][0]["text"], "Valdivar firmó y Valdivar pagó.")
        undone = gc.undo(corrected, "c0001")
        self.assertEqual(undone["segments"][0]["text"], "Valdivar firmó y Baldibar pagó.")
        self.assertEqual(undone["turns"][0]["text"], "Valdivar firmó y Baldibar pagó.")

    def test_two_corrections_in_one_segment_undo_independently(self):
        corrected, _, _ = gc.correct(transcript("Baldibar y Luservo firman con Baldibar."), TERMS, Model(), 1)
        self.assertEqual(corrected["segments"][0]["text"], "Valdivar y Lucerno firman con Valdivar.")
        undone = gc.undo(corrected, "c0003")
        self.assertEqual(undone["segments"][0]["text"], "Valdivar y Lucerno firman con Baldibar.")
        undone = gc.undo(undone, "c0001")
        self.assertEqual(undone["segments"][0]["text"], "Baldibar y Lucerno firman con Baldibar.")
        self.assertEqual(undone["turns"][0]["text"], undone["segments"][0]["text"])

    def test_turn_spanning_segments_edits_the_same_occurrence(self):
        raw = transcript("Hablamos con Baldibar.", "Baldibar confirma.")
        raw["turns"] = [{"id": "t000000", "text": "Hablamos con Baldibar. Baldibar confirma.", "start_ms": 0, "end_ms": 1900,
                         "segment_ids": ["s000000", "s000001"], "word_count": 5}]
        model = Model()
        found = gc.preselect(raw, TERMS)
        second = next(h["id"] for h in found if h["segment_id"] == "s000001")
        corrected = gc.apply(raw, found, {second}, 1)
        self.assertEqual(corrected["turns"][0]["text"], "Hablamos con Baldibar. Valdivar confirma.")
        self.assertEqual(gc.undo(corrected, "c0001")["turns"][0]["text"], "Hablamos con Baldibar. Baldibar confirma.")
        self.assertFalse(model.calls)

    def test_alias_that_is_a_common_word_is_not_a_candidate(self):
        terms = [{"id": "g9", "term": "Lucerno", "aliases": ["cosas"]}]
        self.assertEqual(candidates("hay muchas cosas que revisar", terms), [])


class MiningTests(unittest.TestCase):
    def test_only_uncertain_spellings_are_proposed(self):
        text = transcript("Mañana vemos a Baldibar y a Atlantia.", "Atlantia confirma la fecha con Baldibar.")
        proposals = gc.mine(text, TERMS, [{"heard": "Luservo", "guess": "Lucerno Group"}])
        by_term = {p["term"]: p for p in proposals}
        self.assertEqual((by_term["Baldibar"]["origin"], by_term["Baldibar"]["alias_of"], by_term["Baldibar"]["auto"]), ("fonetico", "g1", False))
        self.assertEqual(by_term["Lucerno Group"]["origin"], "llm")
        # A repeated capitalized word is not a term by itself: only the model, with a definition, adds it.
        self.assertNotIn("Atlantia", by_term)

    def test_fillers_participants_and_folder_are_not_proposed(self):
        text = transcript("¿Vale? Vale, perfecto. Venga, Vale.", "Vale. Ana Pérez cierra.")
        self.assertEqual(gc.mine(text, TERMS, []), [])

    def test_approved_terms_and_common_words_are_not_proposed(self):
        proposals = gc.mine(transcript("Valdivar y Nimbus. Consumo alto."), TERMS, [])
        self.assertEqual(proposals, [])


if __name__ == "__main__":
    unittest.main()


class AliasCorrectionTests(unittest.TestCase):
    """registered «sounds like» aliases become their term without a model, like a custom dictionary; an alias
    that is also a real word only suggests, unless the meeting's context or people's answers settle it."""

    ALIASED = [{"id": "g1", "term": "Valdivar", "aliases": ["Baldibar", "val divar"]},
               {"id": "g2", "term": "OmniRoute", "aliases": ["Omni Ruth", "Ruth"]},             # «Ruth» alone is a name
               {"id": "g3", "term": "Synth", "aliases": ["Vale", "PH", "synth"]},       # ordinary word, too short, own name
               {"id": "g4", "term": "Lucerno", "aliases": ["Luzerno"]},
               {"id": "g5", "term": "Lucerna", "aliases": ["Luzerno"]}]                 # shared: means neither for sure

    def test_whole_words_and_phrases_become_the_term_keeping_case_insensitive_matches_and_punctuation(self):
        from synth.contracts.transcript import Transcript
        source = transcript("Mañana con baldibar, y luego Omni   Ruth.", "El baldibarsmo no es un nombre; BALDIBAR sí.")
        corrected = gc.correct_aliases(source, self.ALIASED, 1)
        self.assertEqual([s["text"] for s in corrected["segments"]],
                         ["Mañana con Valdivar, y luego OmniRoute.", "El baldibarsmo no es un nombre; Valdivar sí."])
        self.assertEqual(corrected["turns"][0]["text"], "Mañana con Valdivar, y luego OmniRoute.")
        self.assertEqual({(c["origin"], c["status"]) for c in corrected["corrections"]}, {("alias", "applied")})
        self.assertEqual(corrected["raw_version"], 1)
        Transcript.model_validate(corrected)
        self.assertEqual(source["segments"][0]["text"], "Mañana con baldibar, y luego Omni   Ruth.")  # raw stays as heard

    def test_each_alias_has_a_mode_and_a_reason(self):
        names = {"valdivar", "omniroute", "synth"}
        self.assertEqual(gc.alias_mode("Baldibar", None, names), ("corrige", "inventado"))
        self.assertEqual(gc.alias_mode("Cinta", None, names), ("sugiere", "palabra"))
        self.assertEqual(gc.alias_mode("Vale", None, names), ("sugiere", "palabra"))
        self.assertEqual(gc.alias_mode("PH", None, names), ("inactivo", "corto"))
        self.assertEqual(gc.alias_mode("synth", None, names), ("inactivo", "nombre"))
        self.assertEqual(gc.alias_mode("Luzerno", None, names, shared=True), ("inactivo", "compartido"))

    def test_peoples_answers_move_an_alias_one_step(self):
        self.assertEqual(gc.alias_mode("Cinta", {"cinta": {"si": 2}}), ("corrige", "aceptado"))
        self.assertEqual(gc.alias_mode("Cinta", {"cinta": {"si": 2, "no": 1}}), ("sugiere", "palabra"))
        self.assertEqual(gc.alias_mode("Cinta", {"cinta": {"no": 2}}), ("inactivo", "rechazado"))
        self.assertEqual(gc.alias_mode("Baldibar", {"baldibar": {"no": 2}}), ("sugiere", "rechazado"))

    def test_a_real_word_alias_suggests_and_never_rewrites_the_lighthouse_or_a_person(self):
        terms = [{"term": "Synth", "aliases": ["Cinta"]}, {"term": "Claude", "aliases": ["Claudio"]},
                 {"term": "Nimbus", "aliases": ["Nimvus"]}]
        corrected = gc.correct_aliases(transcript("Visitamos el Cinta de Finisterre con Claudio y luego Nimvus."), terms, 1)
        self.assertEqual(corrected["segments"][0]["text"], "Visitamos el Cinta de Finisterre con Claudio y luego Nimbus.")
        self.assertEqual(sorted((c["original"], c["status"]) for c in corrected["corrections"]),
                         [("Cinta", "suggested"), ("Claudio", "suggested"), ("Nimvus", "applied")])

    def test_a_meeting_that_names_the_term_settles_its_ambiguous_alias(self):
        corrected = gc.correct_aliases(transcript("Hoy demo de Synth.", "En Cinta ya funciona el glosario."),
                                       [{"term": "Synth", "aliases": ["Cinta"]}], 1)
        self.assertEqual(corrected["segments"][1]["text"], "En Synth ya funciona el glosario.")
        self.assertEqual([(c["origin"], c["status"]) for c in corrected["corrections"]], [("contexto", "applied")])

    def test_a_suggestion_is_accepted_where_it_was_recorded_and_then_can_be_undone(self):
        from synth.contracts.transcript import Transcript
        source = transcript("El Cinta y Baldibar, otra vez Cinta.")
        terms = [{"term": "Synth", "aliases": ["Cinta"]}, {"term": "Valdivar", "aliases": ["Baldibar"]}]
        corrected = gc.correct_aliases(source, terms, 1)
        Transcript.model_validate(corrected)
        second = next(c for c in corrected["corrections"] if c["status"] == "suggested" and c["start"] > 10)
        accepted = gc.accept(corrected, second["id"])
        self.assertEqual(accepted["segments"][0]["text"], "El Cinta y Valdivar, otra vez Synth.")
        self.assertEqual(accepted["turns"][0]["text"], "El Cinta y Valdivar, otra vez Synth.")
        with self.assertRaises(ValueError):
            gc.accept(accepted, second["id"])
        self.assertEqual(gc.undo(accepted, second["id"])["segments"][0]["text"], "El Cinta y Valdivar, otra vez Cinta.")

    def test_inactive_aliases_never_appear_and_an_alias_correction_can_be_undone(self):
        self.assertIsNone(gc.correct_aliases(transcript("PH y Luzerno, como dijo synth."), self.ALIASED, 1))
        corrected = gc.correct_aliases(transcript("Hablamos con Baldibar."), self.ALIASED, 1)
        self.assertEqual(gc.undo(corrected, corrected["corrections"][0]["id"])["segments"][0]["text"], "Hablamos con Baldibar.")

    def test_nothing_happens_without_aliases_or_on_an_already_corrected_version(self):
        self.assertIsNone(gc.correct_aliases(transcript("Hablamos con Baldibar."), TERMS[:2], 1))
        once = gc.correct_aliases(transcript("Hablamos con Baldibar."), self.ALIASED, 1)
        self.assertIsNone(gc.correct_aliases(once, self.ALIASED, 2))

    def test_without_the_lexicon_every_alias_only_suggests(self):
        import builtins
        real_import = builtins.__import__
        def no_wordfreq(name, *args, **kwargs):
            if name == "wordfreq":
                raise ImportError(name)
            return real_import(name, *args, **kwargs)
        with unittest.mock.patch("builtins.__import__", side_effect=no_wordfreq):
            self.assertEqual({mode for _, mode in gc.alias_table(self.ALIASED).values()}, {"sugiere"})
