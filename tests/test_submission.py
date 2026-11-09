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
from solution import read_questions, run, write_submission


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
            context = json.loads(payload['messages'][-1]['content'])
            seen.append(context['dialogue'])
            self.assertTrue(context['knowledge_fragments'])
            return httpx.Response(200, json={'choices': [{'message': {'content': 'Ответ, с "кавычками"\nНовая строка [1]'}}]})
        errors = run(self.root, self.root / 'out.csv', self.settings, self.client(handler))
        self.assertEqual(errors, 0)
        self.assertCountEqual(seen, ['Датчики Альфа?', 'Батареи Бета?'])
        with (self.root / 'out.csv').open(encoding='utf-8', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([r['id'] for r in rows], ['007', 'two'])
        self.assertEqual(rows[0]['answer'], 'Ответ, с "кавычками"\nНовая строка [1]')

    def test_duplicate_ids_are_rejected(self):
        self.questions([('1', 'a'), ('1', 'b')])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            read_questions(self.root / 'questions.csv')

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
        self.assertIn('не удалось', (self.root / 'out.csv').read_text(encoding='utf-8'))

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

    def test_nonfinite_parameters_rejected(self):
        with patch.dict('os.environ', {'LLM_BASE_URL': 'https://example.invalid', 'LLM_API_KEY': 'test', 'LLM_MODEL': 'test', 'TEMPERATURE': 'nan'}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()


if __name__ == '__main__':
    unittest.main()
