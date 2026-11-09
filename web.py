"""Local web prototype. Run one Uvicorn worker; public hosting requires hardening."""
from contextlib import asynccontextmanager
from dataclasses import asdict
import hmac
import os
from pathlib import Path
import re
import threading
import time
from zipfile import BadZipFile
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from consultant import Consultant
from model_client import ModelClient, ProviderError, Settings
from retrieval import SearchIndex, load_archive
from storage import Store

ROOT = Path(__file__).resolve().parent


class MessageInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)

    @field_validator('text')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Введите текст')
        return value.strip()


class LeadInput(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    phone: str = Field(min_length=10, max_length=25)
    company: str = Field(min_length=2, max_length=200)
    test_data_confirmed: bool

    @field_validator('name', 'company')
    @classmethod
    def nonblank(cls, value):
        if len(value.strip()) < 2:
            raise ValueError('Поле слишком короткое')
        return value.strip()

    @field_validator('phone')
    @classmethod
    def valid_phone(cls, value):
        if not re.fullmatch(r'\+?[\d ()-]+', value) or not 10 <= len(re.sub(r'\D', '', value)) <= 15:
            raise ValueError('Некорректный телефон')
        return value


def create_app(core=None, db_path: Path | None = None, admin_token: str | None = None) -> FastAPI:
    store = Store(db_path or Path(os.environ.get('STATE_DIR', 'work/state')) / 'sessions.sqlite')
    locks = [threading.Lock() for _ in range(64)]
    slots = threading.BoundedSemaphore(3)
    admin = admin_token if admin_token is not None else os.environ.get('ADMIN_TOKEN', '')
    model_client = None
    status = {'knowledge_base_ready': core is not None, 'model_configured': core is not None,
              'provider_verified': False, 'model': None}

    @asynccontextmanager
    async def lifespan(app):
        nonlocal core, model_client
        if core is None:
            settings = index = None
            try:
                settings = Settings.from_env()
            except ValueError:
                pass
            else:
                status['model_configured'] = True
                status['model'] = settings.model
            try:
                index = SearchIndex(load_archive(str(Path(os.environ.get('DATA_DIR', 'data')) / 'knowledge-base.zip')))
            except (ValueError, OSError, BadZipFile):
                pass
            else:
                status['knowledge_base_ready'] = True
            if settings is not None and index is not None:
                model_client = ModelClient(settings)
                core = Consultant(index, model_client)
        app.state.ready = core is not None
        try:
            yield
        finally:
            if model_client:
                model_client.close()

    app = FastAPI(title='EnergyTool Consultant', lifespan=lifespan)

    def require_session(identifier):
        session = store.session(identifier)
        if session is None:
            raise HTTPException(404, 'Диалог не найден')
        return session

    def require_admin(authorization):
        if not admin or not authorization or not hmac.compare_digest(authorization, 'Bearer ' + admin):
            raise HTTPException(401, 'Нужен ключ проверяющего')

    @app.get('/api/health')
    def health():
        return {'ready': core is not None, 'mode': 'local-demo', **status}

    @app.post('/api/sessions', status_code=201)
    def new_session():
        return {'id': store.create_session()}

    @app.get('/api/sessions/{identifier}')
    def session_detail(identifier: str):
        return require_session(identifier)

    @app.post('/api/sessions/{identifier}/messages')
    def message(identifier: str, body: MessageInput):
        if not slots.acquire(blocking=False):
            raise HTTPException(429, 'Консультант занят. Повторите запрос чуть позже.')
        try:
            with locks[hash(identifier) % len(locks)]:
                session = require_session(identifier)
                history = [m for m in session['messages'] if m['role'] in {'user', 'assistant'}]
                dialogue = '\n'.join(('Пользователь' if m['role'] == 'user' else 'Ассистент') + ': ' + m['text'] for m in history)
                dialogue += '\nПользователь: ' + body.text
                if len(dialogue) > 60000:
                    raise HTTPException(422, 'Диалог достиг лимита. Начните новый диалог; история сохранится.')
                store.message(identifier, 'user', body.text)
                if core is None:
                    missing = []
                    if not status['model_configured']:
                        missing.append('API модели')
                    if not status['knowledge_base_ready']:
                        missing.append('базу знаний')
                    detail = 'Для консультации нужно подключить ' + ' и '.join(missing) + '.'
                    store.message(identifier, 'error', detail)
                    raise HTTPException(503, detail)
                try:
                    answer = core.answer(dialogue, time.monotonic() + 150)
                except ProviderError:
                    store.message(identifier, 'error', 'Сервис модели временно недоступен')
                    raise HTTPException(503, 'Сервис модели временно недоступен. Попробуйте позже.') from None
                store.message(identifier, 'assistant', answer.answer, {'sources': answer.sources, 'elapsed_ms': answer.elapsed_ms})
                if model_client is not None:
                    status['provider_verified'] = True
                return asdict(answer)
        finally:
            slots.release()

    @app.post('/api/sessions/{identifier}/lead', status_code=201)
    def lead(identifier: str, body: LeadInput):
        if not body.test_data_confirmed:
            raise HTTPException(422, 'Используйте только выдуманные контакты и подтвердите это')
        with locks[hash(identifier) % len(locks)]:
            session = require_session(identifier)
            if session['lead']:
                existing = session['lead']
                if any(existing[key] != getattr(body, key) for key in ('name', 'phone', 'company')):
                    raise HTTPException(409, 'Заявка уже сохранена с другими контактами')
                return existing
            users = [m['text'] for m in session['messages'] if m['role'] == 'user']
            assistants = [m['text'] for m in session['messages'] if m['role'] == 'assistant']
            if not users or not assistants:
                raise HTTPException(409, 'Для заявки сначала пройдите консультацию')
            summary = 'Задача и уточнения клиента: ' + ' / '.join(users)[-2000:]
            summary += '\nПоследняя консультация: ' + assistants[-1][:1800]
            return store.lead(identifier, body.name, body.phone, body.company, summary)

    @app.get('/api/admin/sessions')
    def archive(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return store.recent()

    @app.get('/')
    def home():
        return FileResponse(ROOT / 'static/index.html')

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    return app
