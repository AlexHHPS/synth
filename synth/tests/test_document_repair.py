import json
import unittest
from unittest.mock import patch
from synth.worker.document import generate, bind_evidence


class DocumentRepairTests(unittest.TestCase):
    def setUp(self):
        self.transcript = {"segments": [{"id": "s0", "text": "Decidimos probar mañana"}]}
        self.valid = {"language": "es", "summary": [{"text": "Prueba pendiente",
            "evidence": [{"segment_id": "s0", "quote": "probar mañana"}]}],
            "decisions": [], "actions": [], "open_questions": []}

    def response(self, document):
        return {"content": json.dumps(document), "effective_model": "synthetic:test"}

    def test_schema_failure_gets_one_validated_correction_and_only_valid_checkpoint(self):
        invalid = {**self.valid, "summary": [{**self.valid["summary"][0], "extra": "wrong"}]}
        checkpoints = []
        with patch("synth.worker.document.completion", side_effect=[self.response(invalid), self.response(self.valid)]) as provider:
            content, _, responses = generate(self.transcript, "", save_checkpoint=checkpoints.append)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(content, self.valid)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(len(responses), 1)
        self.assertIn("document_item_schema", provider.call_args.args[0][-1]["content"])

    def test_invalid_citation_remains_rejected_after_bounded_retry(self):
        invalid = {**self.valid, "summary": [{"text": "Inventado", "evidence": [{"segment_id": "s0", "quote": "no existe"}]}]}
        checkpoints = []
        with patch("synth.worker.document.completion", return_value=self.response(invalid)) as provider:
            with self.assertRaisesRegex(ValueError, "document_invalid_citation"):
                generate(self.transcript, "", save_checkpoint=checkpoints.append)
        self.assertEqual(provider.call_count, 2)
        self.assertFalse(checkpoints)

    def test_valid_response_does_not_retry(self):
        with patch("synth.worker.document.completion", return_value=self.response(self.valid)) as provider:
            generate(self.transcript, "")
        self.assertEqual(provider.call_count, 1)

    def test_invalid_json_is_bounded_and_never_checkpointed(self):
        with patch("synth.worker.document.completion", return_value={"content": "not JSON"}) as provider:
            with self.assertRaisesRegex(ValueError, "document_json_invalid"):
                generate(self.transcript, "")
        self.assertEqual(provider.call_count, 2)

    def test_id_only_evidence_is_bound_to_exact_original_words(self):
        source = {**self.valid, 'summary': [{'text': 'Prueba pendiente', 'evidence': [{'segment_id': 's0'}]}]}
        result = bind_evidence(source, self.transcript)
        self.assertEqual(result['summary'][0]['evidence'][0]['quote'], 'Decidimos probar mañana')
        self.assertNotIn('quote', source['summary'][0]['evidence'][0])

    def test_unknown_segment_cannot_be_used_as_evidence(self):
        source = {**self.valid, 'summary': [{'text': 'Prueba pendiente', 'evidence': [{'segment_id': 'invented'}]}]}
        with self.assertRaisesRegex(ValueError, 'document_invalid_citation'):
            bind_evidence(source, self.transcript)

    def test_owner_must_still_appear_in_the_bound_source(self):
        source = {**self.valid, 'actions': [{'text': 'Probar', 'owner': 'Invented owner', 'due_date': None,
                                           'evidence': [{'segment_id': 's0'}]}]}
        with self.assertRaisesRegex(ValueError, 'document_owner_unsupported'):
            bind_evidence(source, self.transcript)
