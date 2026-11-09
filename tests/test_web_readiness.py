from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import httpx
from fastapi.testclient import TestClient
from model_client import ModelClient
from web import create_app


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.env = {'DATA_DIR': str(self.root)}
        self.db = self.root / 'sessions.sqlite'

    def write_base(self):
        with ZipFile(self.root / 'knowledge-base.zip', 'w') as archive:
            archive.writestr('fixture.md', '# Учёт\nТестовый прибор измеряет расход энергии.')

    def configure_model(self):
        self.env.update(LLM_BASE_URL='https://example.invalid/v1', LLM_API_KEY='synthetic-secret', LLM_MODEL='fixture')

    def send(self, client):
        sid = client.post('/api/sessions').json()['id']
        return client.post(f'/api/sessions/{sid}/messages', json={'text': 'Как измерить расход энергии?'})

    def test_loaded_base_is_reported_without_model_configuration(self):
        self.write_base()
        with patch.dict('os.environ', self.env, clear=True), TestClient(create_app(db_path=self.db)) as client:
            health = client.get('/api/health').json()
            self.assertTrue(health['knowledge_base_ready'])
            self.assertFalse(health['model_configured'])
            self.assertFalse(health['ready'])
            response = self.send(client)
            self.assertEqual(response.status_code, 503)
            self.assertIn('API модели', response.json()['detail'])
            self.assertNotIn('базу знаний', response.json()['detail'])

    def test_missing_base_does_not_hide_valid_model_configuration(self):
        self.configure_model()
        with patch.dict('os.environ', self.env, clear=True), TestClient(create_app(db_path=self.db)) as client:
            health = client.get('/api/health').json()
            self.assertFalse(health['knowledge_base_ready'])
            self.assertTrue(health['model_configured'])
            self.assertFalse(health['ready'])
            self.assertEqual(health['model'], 'fixture')
            self.assertNotIn('synthetic-secret', str(health))
            self.assertNotIn('example.invalid', str(health))
            self.assertIn('базу знаний', self.send(client).json()['detail'])

    def test_provider_verified_requires_success_and_resets_on_restart(self):
        self.write_base()
        self.configure_model()
        failing = True
        def handler(request):
            if failing:
                return httpx.Response(401)
            return httpx.Response(200, json={'choices': [{'message': {'content': 'Тестовый ответ.'}}]})
        def factory(settings):
            return ModelClient(settings, transport=httpx.MockTransport(handler))
        with patch.dict('os.environ', self.env, clear=True), patch('web.ModelClient', side_effect=factory):
            with TestClient(create_app(db_path=self.db)) as client:
                self.assertTrue(client.get('/api/health').json()['ready'])
                self.assertFalse(client.get('/api/health').json()['provider_verified'])
                self.assertEqual(self.send(client).status_code, 503)
                self.assertFalse(client.get('/api/health').json()['provider_verified'])
                failing = False
                self.assertEqual(self.send(client).status_code, 200)
                self.assertTrue(client.get('/api/health').json()['provider_verified'])
            with TestClient(create_app(db_path=self.db)) as restarted:
                self.assertFalse(restarted.get('/api/health').json()['provider_verified'])
