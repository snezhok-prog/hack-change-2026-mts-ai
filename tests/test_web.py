from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from consultant import Consultation
from model_client import ProviderError
from web import create_app


class RecordingCore:
    def __init__(self):
        self.dialogues = []
        self.fail = False

    def answer(self, dialogue, deadline):
        self.dialogues.append(dialogue)
        if self.fail:
            raise ProviderError('Provider HTTP 401')
        return Consultation('Синтетическая консультация [1]', ({'id': 1, 'document': 'fixture.md', 'heading': 'Тест', 'text': 'Тестовый факт', 'score': 1.0},), 5)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = Path(self.folder.name) / 'sessions.sqlite'
        self.core = RecordingCore()
        self.client = self.enterContext(TestClient(create_app(self.core, self.db, 'test-admin')))
        self.sid = self.client.post('/api/sessions').json()['id']

    def send(self, text='Тестовый вопрос'):
        return self.client.post(f'/api/sessions/{self.sid}/messages', json={'text': text})

    def test_conversations_are_isolated_and_history_persists(self):
        self.assertEqual(self.send('Первая задача').status_code, 200)
        other = self.client.post('/api/sessions').json()['id']
        self.client.post(f'/api/sessions/{other}/messages', json={'text': 'Другая задача'})
        self.assertNotIn('Первая задача', self.core.dialogues[-1])
        self.send('Уточнение')
        self.assertIn('Первая задача', self.core.dialogues[-1])
        with TestClient(create_app(self.core, self.db, 'test-admin')) as restarted:
            stored = restarted.get(f'/api/sessions/{self.sid}').json()
            self.assertEqual(len(stored['messages']), 4)
            self.assertIsNone(stored['lead'])
            self.assertEqual(stored['messages'][1]['metadata']['elapsed_ms'], 5)

    def test_provider_failure_preserves_question_without_fake_answer(self):
        self.core.fail = True
        self.assertEqual(self.send().status_code, 503)
        stored = self.client.get(f'/api/sessions/{self.sid}').json()
        self.assertEqual([m['role'] for m in stored['messages']], ['user', 'error'])
        self.core.fail = False
        self.send('Повторный запрос')
        self.assertNotIn('временно недоступен', self.core.dialogues[-1])

    def test_lead_validation_summary_and_idempotence(self):
        path = f'/api/sessions/{self.sid}/lead'
        body = {'name': 'Тестовый Клиент', 'phone': '+7 (000) 000-00-00', 'company': 'Выдуманная компания', 'test_data_confirmed': True}
        self.assertEqual(self.client.post(path, json=body).status_code, 409)
        self.send('Нужен тестовый датчик')
        self.assertEqual(self.client.post(path, json={**body, 'test_data_confirmed': False}).status_code, 422)
        self.assertEqual(self.client.post(path, json={**body, 'phone': 'not-a-phone'}).status_code, 422)
        first = self.client.post(path, json=body)
        self.assertEqual(first.status_code, 201)
        self.assertIn('тестовый датчик', first.json()['summary'])
        self.assertEqual(first.json()['id'], self.client.post(path, json=body).json()['id'])
        self.assertEqual(self.client.post(path, json={**body, 'name': 'Другой Клиент'}).status_code, 409)

    def test_archive_requires_admin_and_includes_sessions_without_leads(self):
        self.assertEqual(self.client.get('/api/admin/sessions').status_code, 401)
        self.assertEqual(self.client.get('/api/admin/sessions', headers={'Authorization': 'Bearer wrong'}).status_code, 401)
        response = self.client.get('/api/admin/sessions', headers={'Authorization': 'Bearer test-admin'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]['id'], self.sid)
        self.assertEqual(response.json()[0]['has_lead'], 0)

    def test_missing_config_returns_honest_unavailability(self):
        with patch.dict('os.environ', {}, clear=True):
            with TestClient(create_app(db_path=self.db)) as client:
                self.assertFalse(client.get('/api/health').json()['ready'])
                self.assertEqual(client.post(f'/api/sessions/{self.sid}/messages', json={'text': 'Вопрос'}).status_code, 503)

    def test_validation_unknown_session_and_static_ui(self):
        self.assertEqual(self.send('   ').status_code, 422)
        self.assertEqual(self.send('x' * 4001).status_code, 422)
        self.assertEqual(self.client.get('/api/sessions/missing').status_code, 404)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/static/app.js').status_code, 200)


if __name__ == '__main__':
    unittest.main()
