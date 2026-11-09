"""Organizer-compatible chat API with explicit configuration and bounded retries."""
from dataclasses import dataclass, field
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import math
import os
import threading
import time
from urllib.parse import urlsplit
import httpx


class ProviderError(RuntimeError):
    """Safe error message: never contains response body, URL credentials or keys."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    max_tokens: int = 1200
    temperature: float = 0.0
    timeout: float = 45.0
    workers: int = 3
    top_k: int = 8

    @classmethod
    def from_env(cls) -> 'Settings':
        """Platform-tolerant: unset, empty or placeholder values (VALUE == NAME) fall back to
        defaults; numbers are clamped instead of rejected. Only a missing endpoint/key or an
        unsafe URL raises, and the batch entrypoint still writes a complete CSV in that case."""
        base = env_first('LLM_BASE_URL', 'OPENAI_BASE_URL', 'BASE_URL').rstrip('/')
        key = env_first('LLM_API_KEY', 'OPENAI_API_KEY')
        # Same default as the organizer sample; the platform normally provides the ID.
        model = env_first('LLM_MODEL', 'OPENAI_MODEL', 'GEN_MODEL') or DEFAULT_MODEL
        if not base or not key:
            raise ValueError('Set LLM_BASE_URL and LLM_API_KEY (or documented aliases)')
        parsed = urlsplit(base)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('LLM_BASE_URL must be an HTTP(S) base URL without credentials, query or fragment')
        return cls(base, key, model,
                   env_number('MAX_TOKENS', 1200, 128, 8192, int),
                   env_number('TEMPERATURE', 0.0, 0.0, 2.0, float),
                   env_number('REQUEST_TIMEOUT', 45.0, 1.0, 120.0, float),
                   env_number('MAX_WORKERS', 3, 1, 8, int),
                   env_number('TOP_K', 8, 1, 30, int))


DEFAULT_MODEL = 'qwen/qwen3.8-27b'


def env_value(name: str) -> str:
    """Environment value, treating empty strings and unfilled placeholders (VALUE == NAME) as unset."""
    value = os.environ.get(name, '').strip()
    return '' if value == name else value


def env_first(*names: str) -> str:
    return next((v for v in map(env_value, names) if v), '')


def env_number(name: str, default, lower, upper, kind):
    try:
        value = kind(float(env_value(name))) if env_value(name) else default
    except (ValueError, OverflowError):
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    return max(lower, min(upper, value))


class ModelClient:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.client = httpx.AsyncClient(transport=transport, timeout=settings.timeout,
                                  limits=httpx.Limits(max_connections=settings.workers, max_keepalive_connections=settings.workers))
        # One event loop owns the shared pool; synchronous callers can cancel I/O.
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name='model-http', daemon=True)
        self.thread.start()
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            asyncio.run_coroutine_threadsafe(self._close(), self.loop).result(timeout=5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            if not self.thread.is_alive():
                self.loop.close()

    async def _close(self) -> None:
        pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self.client.aclose()

    def generate(self, messages: list[dict[str, str]], deadline: float) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError('Run time budget exhausted')
        if self.closed:
            raise ProviderError('Model client is closed')
        future = asyncio.run_coroutine_threadsafe(self._generate(messages, deadline), self.loop)
        try:
            return future.result(timeout=remaining)
        except FutureTimeout:
            future.cancel()
            raise ProviderError('Run time budget exhausted') from None

    async def _generate(self, messages: list[dict[str, str]], deadline: float) -> str:
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError('Run time budget exhausted')
            try:
                response = await self.client.post(self.settings.base_url + '/chat/completions',
                    headers={'Authorization': 'Bearer ' + self.settings.api_key},
                    json={'model': self.settings.model, 'messages': messages,
                          'temperature': self.settings.temperature, 'max_tokens': self.settings.max_tokens},
                    timeout=httpx.Timeout(min(self.settings.timeout, remaining), connect=min(10, remaining)))
            except httpx.TransportError:
                if attempt == 2:
                    raise ProviderError('Provider transport failure') from None
            else:
                if response.status_code not in {408, 429, 500, 502, 503, 504}:
                    if not response.is_success:
                        raise ProviderError(f'Provider HTTP {response.status_code}')
                    try:
                        answer = response.json()['choices'][0]['message']['content']
                    except (ValueError, KeyError, IndexError, TypeError):
                        raise ProviderError('Invalid provider response schema') from None
                    if not isinstance(answer, str) or not answer.strip():
                        raise ProviderError('Provider returned an empty answer')
                    if len(answer.strip()) > 10000:
                        raise ProviderError('Provider answer exceeds 10000 characters')
                    return answer.strip()
                if attempt == 2:
                    raise ProviderError(f'Provider HTTP {response.status_code} after retries')
            delay = min(.5 * (2 ** attempt), max(0, deadline - time.monotonic()))
            await asyncio.sleep(delay)
        raise ProviderError('Provider did not return an answer')
