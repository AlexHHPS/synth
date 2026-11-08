import copy
import unittest

from synth.worker.semantic_state import checkpoint, messages, restore, validate


class SemanticStateTests(unittest.TestCase):
    def setUp(self):
        self.sources = [{"id": "s1", "text": "Se propone publicar; no acordamos fecha."},
                        {"id": "s2", "text": "Retiramos la propuesta de publicar."}]
        self.state = {"items": [{"kind": "decision", "text": "Publicación propuesta y retirada.",
            "status": "withdrawn", "owner": None, "due": None, "citations": ["s1", "s2"]}], "markdown": ""}
        self.scope = dict(actor="a", audience=["a"], capture="c", transcript_revision=1, context_revision=3)

    def test_reuse_requires_same_actor_audience_and_revisions(self):
        saved = checkpoint(self.state, self.sources, self.scope)
        self.assertEqual(restore(saved, self.sources, self.scope), self.state)
        for key, value in [("actor", "b"), ("audience", ["a", "b"]), ("capture", "d"),
                           ("transcript_revision", 2), ("context_revision", 4)]:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "semantic_scope_changed"):
                restore(saved, self.sources, {**self.scope, key: value})

    def test_revised_or_removed_citation_invalidates_checkpoint(self):
        saved = checkpoint(self.state, self.sources, self.scope)
        revised = copy.deepcopy(self.sources)
        revised[1]["text"] = "Confirmamos la publicación."
        for sources in (revised, self.sources[:1]):
            with self.assertRaisesRegex(ValueError, "semantic_source_changed"):
                restore(saved, sources, self.scope)

    def test_unrelated_added_source_keeps_bound_state(self):
        saved = checkpoint(self.state, self.sources, self.scope)
        self.assertEqual(restore(saved, self.sources + [{"id": "s3", "text": "Pausa."}], self.scope), self.state)

    def test_missing_duplicate_and_fabricated_citations_rejected(self):
        for citations in ([], ["s9"], ["s1", "s1"], [None]):
            value = copy.deepcopy(self.state)
            value["items"][0]["citations"] = citations
            with self.subTest(citations=citations), self.assertRaises(ValueError):
                validate(value, self.sources)

    def test_never_silently_truncates_oversized_state(self):
        value = {"items": [{**self.state["items"][0], "text": str(i)} for i in range(65)], "markdown": ""}
        with self.assertRaisesRegex(ValueError, "semantic_items_invalid"):
            validate(value, self.sources)

    def test_update_and_final_payloads_are_distinct(self):
        with self.assertRaisesRegex(ValueError, "semantic_markdown_invalid"):
            validate(self.state, self.sources, final=True)
        self.assertEqual(messages(self.sources, final=True)[-1]["role"], "user")


if __name__ == "__main__":
    unittest.main()
