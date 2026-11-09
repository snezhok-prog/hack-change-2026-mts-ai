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
from consultant import Consultant, extractive_answer, tidy
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


class TruncatedReasoningTests(unittest.TestCase):
    def client(self, handler, max_tokens=300):
        client = ModelClient(Settings('https://proxy.invalid/v1', 'k', 'm', max_tokens=max_tokens), transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_cut_off_reasoning_is_retried_once_with_a_larger_budget(self):
        budgets = []
        def handler(request):
            budgets.append(json.loads(request.content)['max_tokens'])
            return reply('<think>долго рассуждаю' if len(budgets) == 1 else '<think>ok</think>Да, можно.')
        self.assertEqual(self.client(handler).generate([], time.monotonic() + 20), 'Да, можно.')
        self.assertEqual(budgets, [1024, 2048])  # floor 1024 even when the platform sets 300

    def test_reasoning_cut_off_twice_fails_without_endless_retries(self):
        calls = []
        def handler(request):
            calls.append(1)
            return reply('<think>бесконечно')
        with self.assertRaisesRegex(ProviderError, 'empty'):
            self.client(handler).generate([], time.monotonic() + 20)
        self.assertEqual(len(calls), 2)


class G021FallbackTests(unittest.TestCase):
    KB = ('# Услуга Сигма\nЦена услуги Сигма составляет 9000 рублей. Услуга Сигма помогает собирать показания. '
          'Ориентир действует только при уже установленных совместимых приборах.')

    def run_batch(self, client, settings=None):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with ZipFile(root / 'knowledge-base.zip', 'w') as archive:
                archive.writestr('s.md', self.KB)
            with (root / 'questions.csv').open('w', encoding='utf-8', newline='') as stream:
                csv.writer(stream).writerows([('id', 'question'), ('1', 'Какова цена услуги Сигма? Не хочу оставлять контакты.')])
            with self.assertLogs('submission', level='INFO'):
                solution.run(root, root / 'out.csv', settings, client)
            with (root / 'out.csv').open(encoding='utf-8', newline='') as stream:
                return next(csv.DictReader(stream))['answer']

    def test_fallback_never_quotes_numbers_or_asks_for_contacts_after_refusal(self):
        settings = Settings('https://x.invalid/v1', 'k', 'm')
        client = ModelClient(settings, transport=httpx.MockTransport(lambda request: httpx.Response(401)))
        self.addCleanup(client.close)
        for answer in (self.run_batch(None), self.run_batch(client, settings)):
            self.assertNotIn('9000', answer)
            self.assertNotIn('заявк', answer)
            self.assertIn('Услуга Сигма помогает собирать показания.', answer)


class C023ContactRefusalTests(unittest.TestCase):
    def test_refusal_word_orders_and_positive_controls(self):
        index = SearchIndex(split_document('s.md', '# Услуга Сигма\nУслуга Сигма помогает собирать показания.'))
        for text in ('Контакты оставлять не буду, заявку не предлагайте. Что умеет услуга Сигма?',
                     'Телефон пока давать не готов. Что умеет услуга Сигма?', 'Без звонков. Что умеет услуга Сигма?'):
            self.assertNotIn('заявк', extractive_answer(index, text), text)
        for text in ('Готов оставить заявку. Что умеет услуга Сигма?', 'Что умеет услуга Сигма?'):
            self.assertIn('заявк', extractive_answer(index, text), text)

    def test_prompt_matches_reference_length_and_lead_policy(self):
        from consultant import SYSTEM
        self.assertIn('30–55 слов', SYSTEM)
        self.assertIn('Не предлагай заявку или звонок в обычном ответе', SYSTEM)
        self.assertIn('утверждай его прямо', SYSTEM)  # references state base facts without hedging


class LengthCutTests(unittest.TestCase):
    def client(self, handler):
        client = ModelClient(Settings('https://proxy.invalid/v1', 'k', 'm'), transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    @staticmethod
    def cut(text):
        return httpx.Response(200, json={'choices': [{'message': {'content': text}, 'finish_reason': 'length'}]})

    def test_cut_answer_is_retried_then_trimmed_to_a_full_sentence(self):
        calls = []
        def handler(request):
            calls.append(json.loads(request.content)['max_tokens'])
            return self.cut('Да, можно. Платформа поддерживает приборы. Для расчёта нам понад')
        self.assertEqual(self.client(handler).generate([], time.monotonic() + 20), 'Да, можно. Платформа поддерживает приборы.')
        self.assertEqual(calls, [1200, 2400])  # default budget, doubled once

    def test_g023_fragment_without_a_sentence_is_an_error_and_budget_never_shrinks(self):
        calls = []
        def handler(request):
            calls.append(json.loads(request.content)['max_tokens'])
            return self.cut('Цена зависит от')
        with self.assertRaisesRegex(ProviderError, 'cut off'):
            self.client(handler).generate([], time.monotonic() + 20)
        self.assertEqual(calls, [1200, 2400])
        big = ModelClient(Settings('https://proxy.invalid/v1', 'k', 'm', max_tokens=8192), transport=httpx.MockTransport(handler))
        self.addCleanup(big.close)
        calls.clear()
        with self.assertRaises(ProviderError):
            big.generate([], time.monotonic() + 20)
        self.assertEqual(calls, [8192])  # cannot grow -> no pointless retry with a smaller budget

    def test_retry_that_completes_wins(self):
        answers = iter([self.cut('Да, можно. Обрыв на полу'), reply('Да, можно. Полный ответ.')])
        self.assertEqual(self.client(lambda request: next(answers)).generate([], time.monotonic() + 20), 'Да, можно. Полный ответ.')


class ExtractiveFallbackTests(unittest.TestCase):
    def test_fallback_quotes_base_facts_and_never_guard_rules(self):
        text = ('# Станции\nСолнечные станции ставим на кровлю. Срок службы около 25 лет. Третье предложение.\n'
                '## Чего утверждать нельзя\nНазывать итоговую цену станции.')
        index = SearchIndex(split_document('solar.md', text))
        answer = extractive_answer(index, 'Солнечные станции на кровлю?')
        self.assertIn('Солнечные станции ставим на кровлю. Третье предложение.', answer)
        self.assertNotIn('25 лет', answer)  # numbers are never quoted without the model (G-021)
        self.assertNotIn('Называть итоговую цену', extractive_answer(index, 'Называть итоговую цену станции?'))
        self.assertTrue(extractive_answer(index, 'рецепт борща').startswith('Чтобы ответить точнее'))


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

    def test_g020_long_guard_reaches_the_model_whole(self):
        guard = ('Синтетическое защитное правило. ' * 85) + 'Обязательное условие: только при установленных приборах.'
        text = f'# Платформа\nПлатформа учёта показаний.\n## Чего утверждать нельзя\n{guard}'
        sent = []
        class FakeClient:
            settings = Settings('https://x.invalid', 'k', 'm', top_k=1)
            def generate(self, messages, deadline):
                sent.append(messages[-1]['content'])
                return 'Ответ.'
        Consultant(SearchIndex(split_document('p.md', text)), FakeClient()).answer('Платформа учёта?', time.monotonic() + 5)
        self.assertIn('Обязательное условие: только при установленных приборах.', sent[0])

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
                if answers.get('1') == 'Ответ Альфа' and answers.get('2') not in (None, 'Ответ Бета'):
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
            self.assertIn('Батареи Бета', seen_in_file[0])  # the pending row holds its base-grounded fallback
            with (root / 'out.csv').open(encoding='utf-8', newline='') as stream:
                self.assertEqual({r['id']: r['answer'] for r in csv.DictReader(stream)}, {'1': 'Ответ Альфа', '2': 'Ответ Бета'})


if __name__ == '__main__':
    unittest.main()
