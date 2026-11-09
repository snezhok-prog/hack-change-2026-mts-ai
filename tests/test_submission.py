import csv
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile
import httpx
from model_client import ModelClient, ProviderError, Settings
from solution import EMPTY_QUESTION, UNAVAILABLE, read_questions, run, write_submission


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.settings = Settings('https://example.invalid/v1', 'test-only-key', 'test-model', workers=2)
        with ZipFile(self.root / 'knowledge-base.zip', 'w') as archive:
            archive.writestr('alpha.md', '# Альфа\nДатчики Альфа считают воду.\n## Ограничения\nТолько помещение.')
            archive.writestr('beta.md', '# Бета\nБатареи Бета питают фонари.')

    def questions(self, rows):
        with (self.root / 'questions.csv').open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['id', 'question'])
            writer.writerows(rows)

    def client(self, handler):
        client = ModelClient(self.settings, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_batch_ids_csv_quoting_and_session_isolation(self):
        self.questions([('007', 'Датчики Альфа?'), ('two', 'Батареи Бета?')])
        seen = []
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload['model'], 'test-model')
            self.assertEqual(request.url.path, '/v1/chat/completions')
            user = payload['messages'][-1]['content']
            self.assertIn('Материалы базы знаний:', user)
            self.assertIn('Датчики Альфа считают воду' if 'Альфа?' in user else 'Батареи Бета питают фонари', user)
            self.assertTrue(user.endswith('/no_think'))
            seen.append(user.split('пользователя):\n', 1)[1].split('\n\n', 1)[0])
            return httpx.Response(200, json={'choices': [{'message': {'content': 'Ответ, с "кавычками"\nНовая строка [1]'}}]})
        errors = run(self.root, self.root / 'out.csv', self.settings, self.client(handler))
        self.assertEqual(errors, 0)
        self.assertCountEqual(seen, ['Датчики Альфа?', 'Батареи Бета?'])
        with (self.root / 'out.csv').open(encoding='utf-8', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([r['id'] for r in rows], ['007', 'two'])
        # Citation markers are stripped; quoting and line breaks survive CSV.
        self.assertEqual(rows[0]['answer'], 'Ответ, с "кавычками"\nНовая строка')

    def test_duplicate_ids_keep_first_and_extra_columns_are_ignored(self):
        with (self.root / 'questions.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow([' ID ', 'Question', 'category'])
            writer.writerows([('1', 'a', 'x'), ('1', 'b', 'y'), ('', 'no id', 'z'), ('2', '', 'w')])
        self.assertEqual(read_questions(self.root / 'questions.csv'), [('1', 'a'), ('2', '')])

    def test_output_validation_preserves_existing_file(self):
        output = self.root / 'out.csv'
        output.write_text('existing', encoding='utf-8')
        with self.assertRaises(ValueError):
            write_submission(output, [('1', '')])
        self.assertEqual(output.read_text(), 'existing')

    def test_auth_failure_not_retried_or_leaked(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(401, text='private-key-must-not-appear')
        with self.assertRaises(ProviderError) as error:
            self.client(handler).generate([], time.monotonic() + 20)
        self.assertEqual(len(calls), 1)
        self.assertEqual(str(error.exception), 'Provider HTTP 401')
        self.assertNotIn('test-only-key', repr(self.settings))

    @patch('model_client.asyncio.sleep')
    def test_transient_failure_retries_are_bounded(self, sleep):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(503)
        with self.assertRaises(ProviderError):
            self.client(handler).generate([], time.monotonic() + 20)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_exhausted_budget_sends_no_requests(self):
        def handler(request):
            self.fail('Expired budget must not issue a request')
        with self.assertRaises(ProviderError):
            self.client(handler).generate([], time.monotonic() - 1)

    def test_provider_error_is_reported_as_failure(self):
        self.questions([('1', 'Датчики?')])
        with self.assertLogs('submission', level='ERROR'):
            failures = run(self.root, self.root / 'out.csv', self.settings, self.client(lambda r: httpx.Response(401)))
        self.assertEqual(failures, 1)
        # Provider failure keeps the base-grounded fallback, never an empty row.
        self.assertIn('Датчики Альфа считают воду.', (self.root / 'out.csv').read_text(encoding='utf-8'))

    def test_invalid_provider_schema_and_empty_answers_fail(self):
        for response in [{}, {'choices': [{'message': {'content': ''}}]}, {'choices': [{'message': {'content': 'x' * 10001}}]}]:
            with self.subTest(response_type=list(response)):
                with self.assertRaises(ProviderError):
                    self.client(lambda r: httpx.Response(200, json=response)).generate([], time.monotonic() + 20)

    def test_environment_requires_model_and_accepts_aliases(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()
        with patch.dict('os.environ', {'OPENAI_BASE_URL': 'https://example.invalid/v1/', 'OPENAI_API_KEY': 'test', 'GEN_MODEL': 'organizer-model'}, clear=True):
            settings = Settings.from_env()
            self.assertEqual(settings.model, 'organizer-model')
            self.assertEqual(settings.base_url, 'https://example.invalid/v1')

    def test_ids_are_echoed_verbatim_and_csv_variants_are_read(self):
        variants = {
            'leading blank line, reversed columns': '\r\n\nquestion,id\r\nАльфа?, 001 \r\n',
            'semicolon delimiter': 'id;question\n 001 ;Альфа?\n',
            'tab delimiter': 'id\tquestion\n 001 \tАльфа?\n',
        }
        for name, content in variants.items():
            with self.subTest(name):
                (self.root / 'questions.csv').write_text(content, encoding='utf-8')
                self.assertEqual(read_questions(self.root / 'questions.csv'), [(' 001 ', 'Альфа?')])

    def test_invalid_numbers_and_placeholders_fall_back_instead_of_aborting(self):
        env = {'LLM_BASE_URL': 'https://example.invalid', 'LLM_API_KEY': 'test', 'LLM_MODEL': 'LLM_MODEL',
               'TEMPERATURE': 'nan', 'MAX_TOKENS': 'MAX_TOKENS', 'TOP_K': '50', 'MAX_WORKERS': '16', 'REQUEST_TIMEOUT': 'inf'}
        with patch.dict('os.environ', env, clear=True):
            settings = Settings.from_env()
        self.assertEqual((settings.model, settings.temperature, settings.max_tokens, settings.top_k, settings.workers, settings.timeout),
                         ('qwen/qwen3.8-27b', 0.0, 1200, 30, 8, 45.0))
        with patch.dict('os.environ', {'BASE_URL': 'https://example.invalid/v1', 'OPENAI_API_KEY': 'test', 'MAX_TOKENS': '16384'}, clear=True):
            settings = Settings.from_env()
        self.assertEqual((settings.base_url, settings.max_tokens), ('https://example.invalid/v1', 8192))

    def read_output(self):
        with (self.root / 'out.csv').open(encoding='utf-8', newline='') as stream:
            return {r['id']: r['answer'] for r in csv.DictReader(stream)}

    def test_missing_model_config_still_writes_every_id_with_base_facts(self):
        self.questions([('1', 'Датчики Альфа?'), ('2', 'Батареи Бета?')])
        failures = run(self.root, self.root / 'out.csv', None, None)
        self.assertEqual(failures, 2)
        output = self.read_output()
        self.assertIn('Датчики Альфа считают воду.', output['1'])
        self.assertIn('Батареи Бета питают фонари.', output['2'])
        self.assertNotIn(UNAVAILABLE, output.values())

    def test_missing_knowledge_base_and_file_aliases(self):
        (self.root / 'knowledge-base.zip').rename(self.root / 'kb.zip')
        with (self.root / 'train.csv').open('w', encoding='utf-8', newline='') as stream:
            csv.writer(stream).writerows([('id', 'question'), ('7', 'Датчики Альфа?')])
        ok = lambda r: httpx.Response(200, json={'choices': [{'message': {'content': 'Ответ'}}]})
        self.assertEqual(run(self.root, self.root / 'out.csv', self.settings, self.client(ok)), 0)
        self.assertEqual(self.read_output(), {'7': 'Ответ'})
        (self.root / 'kb.zip').unlink()
        self.assertEqual(run(self.root, self.root / 'out.csv', self.settings, self.client(ok)), 1)
        self.assertEqual(self.read_output(), {'7': UNAVAILABLE})

    def test_unexpected_error_and_empty_question_do_not_abort_the_batch(self):
        self.questions([('1', 'Датчики Альфа?'), ('2', 'Батареи Бета?'), ('3', '   ')])
        def handler(request):
            if 'Бета' in request.content.decode('utf-8'):
                raise httpx.DecodingError('broken body')  # not a ProviderError
            return httpx.Response(200, json={'choices': [{'message': {'content': 'Ответ'}}]})
        with self.assertLogs('submission', level='ERROR'):
            failures = run(self.root, self.root / 'out.csv', self.settings, self.client(handler))
        self.assertEqual(failures, 1)
        output = self.read_output()
        self.assertEqual((output['1'], output['3']), ('Ответ', EMPTY_QUESTION))
        self.assertIn('Батареи Бета питают фонари.', output['2'])  # base-grounded fallback, not 'unavailable'


if __name__ == '__main__':
    unittest.main()
