"""Core behaviour that affects answer quality: reasoning handling, prompt context, checkpoints."""
import asyncio
import csv
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile
import httpx
from consultant import Consultant, tidy
from model_client import ModelClient, ProviderError, Settings, reasoning_off_params, strip_reasoning
from retrieval import SearchIndex, split_document
import solution


def reply(text):
    return httpx.Response(200, json={'choices': [{'message': {'content': text}}]})


class ReasoningTests(unittest.TestCase):
    def client(self, handler, base='https://proxy.invalid/v1'):
        client = ModelClient(Settings(base, 'test-only-key', 'test-model'), transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_think_blocks_are_removed_and_think_only_answer_is_empty(self):
        self.assertEqual(strip_reasoning('<think>план</think>\nДа, можно.'), 'Да, можно.')
        self.assertEqual(strip_reasoning('рассуждение без открывающего тега</think>Ответ'), 'Ответ')
        self.assertEqual(strip_reasoning('<think>не успела закончить'), '')
        handler = lambda request: reply('<think>долго думаю')
        with self.assertRaisesRegex(ProviderError, 'empty'):
            self.client(handler).generate([], time.monotonic() + 20)

    def test_reasoning_switch_matches_provider(self):
        self.assertEqual(reasoning_off_params('https://openrouter.ai/api/v1'), {'reasoning': {'enabled': False}})
        self.assertEqual(reasoning_off_params('http://proxy:8000/v1'), {'chat_template_kwargs': {'enable_thinking': False}})

    def test_rejected_reasoning_field_is_dropped_once_and_for_all(self):
        bodies = []
        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            return httpx.Response(400) if 'chat_template_kwargs' in body else reply('Ответ')
        client = self.client(handler)
        self.assertEqual(client.generate([], time.monotonic() + 20), 'Ответ')
        self.assertEqual(client.generate([], time.monotonic() + 20), 'Ответ')
        self.assertEqual(['chat_template_kwargs' in b for b in bodies], [True, False, False])


class ContextTests(unittest.TestCase):
    def test_guard_sections_of_retrieved_products_are_added(self):
        text = ('# Станции\nСолнечные станции на кровле.\n## Факты, которые можно утверждать\nСрок службы 25 лет.\n'
                '## Чего утверждать нельзя\nНазывать итоговую цену.')
        chunks = split_document('solar.md', text) + split_document('other.md', '# Учёт\nДатчики воды.\n## Чего утверждать нельзя\nЧужое.')
        sent = []
        class FakeClient:
            settings = Settings('https://x.invalid', 'k', 'm', top_k=1)
            def generate(self, messages, deadline):
                sent.append(messages[-1]['content'])
                return '**Да**, ставим [1]. Какой регион?'
        result = Consultant(SearchIndex(chunks), FakeClient()).answer('Солнечные станции?', time.monotonic() + 5)
        self.assertIn('Солнечные станции на кровле.', sent[0])
        self.assertIn('Называть итоговую цену.', sent[0])
        self.assertNotIn('Чужое.', sent[0])
        self.assertEqual(result.answer, 'Да, ставим. Какой регион?')
        self.assertTrue(any(s.get('guard') for s in result.sources))

    def test_tidy_keeps_wording(self):
        self.assertEqual(tidy('## Итог\n- Да, можно [2, 3].\n- От 750 кВт.'), 'Итог\nДа, можно.\nОт 750 кВт.')


class CheckpointTests(unittest.TestCase):
    def test_finished_answers_reach_csv_before_slow_question_ends(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with ZipFile(root / 'knowledge-base.zip', 'w') as archive:
                archive.writestr('a.md', '# Альфа\nДатчики Альфа.\n# Бета\nБатареи Бета.')
            with (root / 'questions.csv').open('w', encoding='utf-8', newline='') as stream:
                csv.writer(stream).writerows([('id', 'question'), ('1', 'Альфа?'), ('2', 'Бета?')])
            checkpointed, seen_in_file = threading.Event(), []
            real_write = solution.write_submission
            def observed_write(path, rows):
                # Read only after the atomic replace finished, in the writer's own thread (no handle race on Windows).
                real_write(path, rows)
                answers = dict(rows)
                if answers.get('1') == 'Ответ Альфа' and answers.get('2') == solution.UNAVAILABLE:
                    seen_in_file.append(path.read_text(encoding='utf-8'))
                    checkpointed.set()
            async def handler(request):
                # Async: the client runs every request on one event loop, so waiting must not block it.
                if 'Бета?' in request.content.decode('utf-8'):
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and not checkpointed.is_set():
                        await asyncio.sleep(.01)
                    return reply('Ответ Бета')
                return reply('Ответ Альфа')
            settings = Settings('https://example.invalid/v1', 'test-only-key', 'test-model', workers=2)
            client = ModelClient(settings, transport=httpx.MockTransport(handler))
            try:
                with patch.object(solution, 'write_submission', observed_write):
                    self.assertEqual(solution.run(root, root / 'out.csv', settings, client), 0)
            finally:
                client.close()
            self.assertTrue(seen_in_file, 'first answer was not checkpointed while the second was pending')
            self.assertIn(solution.UNAVAILABLE, seen_in_file[0])  # the pending row still has a valid fallback
            with (root / 'out.csv').open(encoding='utf-8', newline='') as stream:
                self.assertEqual({r['id']: r['answer'] for r in csv.DictReader(stream)}, {'1': 'Ответ Альфа', '2': 'Ответ Бета'})


if __name__ == '__main__':
    unittest.main()
