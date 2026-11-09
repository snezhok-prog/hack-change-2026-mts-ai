import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from zipfile import ZipFile
from scripts.build_submission import build, FILES


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def test_deterministic_archive_contains_only_allowed_root_files(self):
        first, second = self.root / 'first.zip', self.root / 'second.zip'
        self.assertEqual(build(first), build(second))
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with ZipFile(first) as archive:
            self.assertEqual(archive.namelist(), list(FILES))
            self.assertIsNone(archive.testzip())
            self.assertLess(first.stat().st_size, 128 * 1024 * 1024)

    def test_extracted_submission_runs_without_repository_on_pythonpath(self):
        archive_path = self.root / 'submission.zip'
        build(archive_path)
        run_dir = self.root / 'isolated'
        with ZipFile(archive_path) as archive:
            archive.extractall(run_dir)
        data = run_dir / 'data'
        data.mkdir()
        with ZipFile(data / 'knowledge-base.zip', 'w') as archive:
            archive.writestr('fixture.md', '# Тестовый датчик\nТестовый датчик Альфа измеряет воду.')
        with (data / 'questions.csv').open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['id', 'question'])
            writer.writerows([['007', 'Что измеряет датчик Альфа?'], ['abc', 'Тестовый датчик?']])
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path, self.headers.get('Authorization'), payload))
                content = json.dumps({'choices': [{'message': {'content': 'Тестовый ответ, с "кавычками"\nи переносом [1]'}}]}, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *args):
                pass  # Synthetic HTTP fixture: keep test output focused on assertions.

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = {k: v for k, v in os.environ.items() if k.upper() not in {
                'PYTHONPATH', 'DATA_DIR', 'LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL',
                'OPENAI_BASE_URL', 'OPENAI_API_KEY', 'OPENAI_MODEL', 'GEN_MODEL',
                'TEMPERATURE', 'MAX_TOKENS', 'MAX_WORKERS', 'TOP_K', 'REQUEST_TIMEOUT', 'RUN_BUDGET_SECONDS'}}
            env.update(LLM_BASE_URL=f'http://127.0.0.1:{server.server_port}/v1', LLM_API_KEY='synthetic-fixture-key',
                       LLM_MODEL='fixture-model', RUN_BUDGET_SECONDS='10', NO_PROXY='127.0.0.1,localhost')
            result = subprocess.run([sys.executable, 'solution.py'], cwd=run_dir, env=env,
                                    capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
            with (run_dir / 'submission.csv').open(encoding='utf-8', newline='') as stream:
                reader = csv.DictReader(stream)
                rows = list(reader)
                self.assertEqual(reader.fieldnames, ['id', 'answer'])
            self.assertEqual([r['id'] for r in rows], ['007', 'abc'])
            self.assertEqual(rows[0]['answer'], 'Тестовый ответ, с "кавычками"\nи переносом [1]')
            self.assertEqual(len(calls), 2)
            for path, authorization, payload in calls:
                self.assertEqual(path, '/v1/chat/completions')
                self.assertEqual(authorization, 'Bearer synthetic-fixture-key')
                self.assertEqual(payload['model'], 'fixture-model')
                self.assertTrue(json.loads(payload['messages'][-1]['content'])['knowledge_fragments'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
