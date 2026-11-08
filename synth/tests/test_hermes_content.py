"""Completed structured continuations retain their original bytes and fields."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import httpx
from synth.hermes.content import response_content
from synth.hermes.transport import Observation

PARTS = ['{"decision":"Revisar los per', 'misos", "owner":null}']
ORIGINAL = ''.join(PARTS)
PROSE = '\n'.join(PARTS)


class ContentTests(unittest.TestCase):
    def test_json_string_split_across_continuations_is_preserved(self):
        with self.assertRaises(ValueError):
            json.loads(PROSE)
        for streamed in (None, [PARTS[0][:6], PARTS[0][6:], PARTS[1]]):
            self.assertEqual(response_content(PROSE, PARTS, streamed), ORIGINAL)
        self.assertEqual(json.loads(ORIGINAL)['decision'], 'Revisar los permisos')

    def test_regular_stream_is_unchanged(self):
        self.assertEqual(response_content('Texto completo.', ['Texto completo.'], ['Texto ', 'completo.']), 'Texto completo.')

    def test_transport_whitespace_is_not_lost(self):
        self.assertEqual(response_content('Texto', [' Texto\n'], [' Texto\n']), ' Texto\n')

    def test_partial_or_replaced_content_is_rejected(self):
        for final, parts, streamed in [(PROSE + ' distinto', PARTS, PARTS),
            (PROSE, PARTS, [ORIGINAL[:-1]]), ('otra respuesta', PARTS, PARTS),
            ('{"x":"par\ncial"', ['{"x":"par', 'cial"'], ['{"x":"par', 'cial"']),
            ('true\nfalse', ['true', 'false'], ['true', 'false'])]:
            with self.subTest(final=final), self.assertRaisesRegex(ValueError, 'hermes_stream_not_contiguous'):
                response_content(final, parts, streamed)

    def test_non_streaming_unrelated_final_is_not_rewritten(self):
        self.assertEqual(response_content('Respuesta revisada.', PARTS), 'Respuesta revisada.')

    def test_complete_restart_after_incomplete_json_is_selected_without_editing_it(self):
        parts=['{"items":[{"quote":"truncated', '{"items":[{"id":"t0"}]}']
        final='\n'.join(parts)
        self.assertEqual(response_content(final,parts),parts[-1])
        with self.assertRaisesRegex(ValueError,'hermes_stream_not_contiguous'):
            response_content(final,parts,parts)

    def test_two_complete_answers_or_unaccounted_final_are_not_silently_replaced(self):
        parts=['{"items":[]}','{"items":[1]}']
        self.assertEqual(response_content('\n'.join(parts),parts),'\n'.join(parts))
        parts=['{"items":[','{"items":[]}']
        self.assertEqual(response_content('different final',parts),'different final')


class ContinuationAgent:
    completed = True

    def __init__(self, **kwargs):
        self.tools = []
        self.callback = kwargs['stream_delta_callback']

    def run_conversation(self, *args, **kwargs):
        index = 0
        def reply(request):
            nonlocal index
            part = PARTS[index]
            index += 1
            return httpx.Response(200, headers={'content-type': 'application/json'}, json={
                'model': 'fixture-model', 'choices': [{'message': {'content': part}}],
                'usage': {'total_tokens': 10}})
        with httpx.Client(transport=httpx.MockTransport(reply)) as client:
            for part in PARTS:
                client.post('http://127.0.0.1:20128/v1/chat/completions')
                if self.callback:
                    self.callback(part)
        return {'completed': self.completed, 'final_response': PROSE, 'served_model': 'fixture-model'}

    def close(self):
        pass


class RunContinuationTests(unittest.TestCase):
    def invoke(self, stream, completed=True):
        fake = types.ModuleType('run_agent')
        fake.AIAgent = ContinuationAgent
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {'run_agent': fake}), \
             patch.object(ContinuationAgent, 'completed', completed), \
             patch.dict(os.environ, {'OMNIROUTE_API_KEY': 'fixture', 'HERMES_RESULT': directory+'/result.json'}):
            spec = importlib.util.spec_from_file_location('synth.hermes.run', Path(__file__).parents[1]/'hermes/run.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            observed = Observation('http://127.0.0.1:20128/v1')
            streamed = []
            module.run({'max_tokens': 100, 'stream': stream}, 'cheap-combo',
                       'http://127.0.0.1:20128/v1', observed, streamed.append, streamed, 'reglas', 'extrae', [])
            return json.loads(Path(directory+'/result.json').read_text())

    def test_run_preserves_structured_output_and_both_attempts(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                result = self.invoke(stream)
                self.assertEqual(result['choices'][0]['message']['content'], ORIGINAL)
                self.assertEqual(len(result['attempts']), 2)
                self.assertEqual([a['usage']['total_tokens'] for a in result['attempts']], [10, 10])
                self.assertNotIn('Revisar', json.dumps(result['attempts']))

    def test_incomplete_run_cannot_publish_even_complete_json(self):
        with self.assertRaisesRegex(ValueError, 'hermes_incomplete'):
            self.invoke(True, completed=False)


if __name__ == '__main__':
    unittest.main()
