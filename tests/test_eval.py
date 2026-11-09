import csv
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile
import httpx
import eval as evaluation
from model_client import ModelClient, Settings


class VerdictAndMetricTests(unittest.TestCase):
    def test_verdict_parsing_handles_think_and_rejects_ambiguity(self):
        self.assertEqual(evaluation.parse_verdict('B>>A'), 'B>>A')
        self.assertEqual(evaluation.parse_verdict('<think>A>B? нет</think>\nМетка: B > A'), 'B>A')
        self.assertEqual(evaluation.parse_verdict('A=B'), 'A=B')
        self.assertIsNone(evaluation.parse_verdict('A>B, хотя возможно B>A'))
        self.assertIsNone(evaluation.parse_verdict('не могу решить'))

    def test_round_scores_mirror_swapped_order(self):
        # Round 1: team is B; round 2: team is A.
        self.assertEqual(evaluation.round_scores('B>>A', 'A>>B'), 1.0)
        self.assertEqual(evaluation.round_scores('A>>B', 'B>>A'), 0.0)
        self.assertEqual(evaluation.round_scores('B>A', 'B>A'), 0.5)
        self.assertEqual(evaluation.round_scores('A=B', 'A>B'), 0.625)

    def test_metrics_follow_organizer_formula(self):
        result = evaluation.metrics([1.0, 0.5, 0.25, 0.75])
        self.assertEqual(result['match_rate'], 0.625)
        self.assertEqual(result['win_rate'], round(2 / 3, 4))
        self.assertEqual(result['score'], round(0.7 * 0.625 + 0.3 * 2 / 3, 4))
        self.assertEqual(evaluation.metrics([0.5, 0.5])['win_rate'], 0.5)

    def test_static_checks_flag_format_risks(self):
        checks = evaluation.static_checks('<think>x</think>**Да**, можно [1]. Какой регион?')
        self.assertTrue(checks['think_leak'])
        self.assertTrue(checks['citations'])
        self.assertTrue(checks['markdown'])
        self.assertEqual(checks['questions'], 1)
        self.assertTrue(evaluation.static_checks('слово ' * 151)['over_150_words'])


class EvalPipelineTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings('https://example.invalid/v1', 'test-only-key', 'test-model', workers=2)

    def client(self, handler):
        client = ModelClient(self.settings, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_judge_swaps_order_and_scores_team(self):
        prompts = []
        def handler(request):
            content = json.loads(request.content)['messages'][-1]['content']
            prompts.append(content)
            team_is_b = content.index('КОМАНДА') > content.index('ОТВЕТ B:')
            return httpx.Response(200, json={'choices': [{'message': {'content': 'B>A' if team_is_b else 'A>B'}}]})
        rows = evaluation.judge([('1', 'Вопрос?')], {'1': 'КОМАНДА'}, {'1': 'эталон'}, {'1': 'база'},
                                self.client(handler), time.monotonic() + 30)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(rows[0]['verdicts'], ['B>A', 'A>B'])
        self.assertEqual(rows[0]['score'], 0.75)

    def test_judge_reports_unparseable_and_missing_answers(self):
        handler = lambda request: httpx.Response(200, json={'choices': [{'message': {'content': 'не знаю'}}]})
        rows = evaluation.judge([('1', 'q'), ('2', 'q')], {'1': 'ответ', '2': ''}, {'1': 'g', '2': 'g'},
                                {'1': 'b', '2': 'b'}, self.client(handler), time.monotonic() + 30)
        self.assertEqual(rows[0]['error'], 'unparseable verdict')
        self.assertIn('missing', rows[1]['error'])
        self.assertEqual(evaluation.summarize(rows)['judged'], 0)

    def test_run_and_judge_cli_with_synthetic_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with ZipFile(root / 'kb.zip', 'w') as archive:
                archive.writestr('alpha.md', '# Альфа\nДатчики Альфа считают воду.')
            for name, column in (('q.csv', 'question'), ('gold.csv', 'answer')):
                with (root / name).open('w', encoding='utf-8', newline='') as stream:
                    writer = csv.writer(stream)
                    writer.writerow(['id', column])
                    writer.writerow(['7', 'Датчики Альфа?' if column == 'question' else 'Считают воду.'])
            def handler(request):
                system = json.loads(request.content)['messages'][0]['content']
                text = 'A=B' if 'эксперт' in system else 'Да, считают воду. Какой у вас объект?'
                return httpx.Response(200, json={'choices': [{'message': {'content': text}}]})
            env = {'LLM_BASE_URL': 'https://example.invalid/v1', 'LLM_API_KEY': 'test-only-key', 'LLM_MODEL': 'test-model'}
            real_client = evaluation.ModelClient
            fake = lambda settings: real_client(settings, transport=httpx.MockTransport(handler))
            with patch.dict('os.environ', env, clear=True), patch.object(evaluation, 'ModelClient', fake), \
                 patch.object(evaluation, 'load_dotenv'), patch('builtins.print'):
                self.assertEqual(evaluation.main(['run', '--questions', str(root / 'q.csv'), '--kb', str(root / 'kb.zip'),
                                                  '--out', str(root / 'v1.csv')]), 0)
                self.assertEqual(evaluation.read_answers(root / 'v1.csv'), {'7': 'Да, считают воду. Какой у вас объект?'})
                self.assertEqual(evaluation.main(['judge', '--answers', str(root / 'v1.csv'), '--questions', str(root / 'q.csv'),
                                                  '--golden', str(root / 'gold.csv')]), 0)
            report = json.loads((root / 'v1.judge.json').read_text(encoding='utf-8'))
            self.assertEqual(report['summary']['match_rate'], 0.5)
            self.assertEqual(report['summary']['baseline'], 'golden')
            self.assertTrue((root / 'v1.details.json').is_file())

    def test_dotenv_does_not_override_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('# c\nLLM_MODEL=from-file\nTEMPERATURE="0.3"\nEMPTY=\n', encoding='utf-8')
            with patch.dict('os.environ', {'LLM_MODEL': 'from-env'}, clear=True):
                evaluation.load_dotenv(path)
                self.assertEqual(os.environ['LLM_MODEL'], 'from-env')
                self.assertEqual(os.environ['TEMPERATURE'], '0.3')
                self.assertNotIn('EMPTY', os.environ)


if __name__ == '__main__':
    unittest.main()
