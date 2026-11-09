"""Organizer-compatible chat API with explicit configuration and bounded retries."""
from dataclasses import dataclass, field
import math
import os
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
        def first(*names: str) -> str:
            return next((os.environ[n].strip() for n in names if os.environ.get(n, '').strip()), '')
        base = first('LLM_BASE_URL', 'OPENAI_BASE_URL').rstrip('/')
        key = first('LLM_API_KEY', 'OPENAI_API_KEY')
        model = first('LLM_MODEL', 'OPENAI_MODEL', 'GEN_MODEL')
        if not base or not key or not model:
            raise ValueError('Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL (or documented aliases)')
        parsed = urlsplit(base)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('LLM_BASE_URL must be an HTTP(S) base URL without credentials, query or fragment')
        result = cls(base, key, model, int(first('MAX_TOKENS') or '1200'), float(first('TEMPERATURE') or '0'), float(first('REQUEST_TIMEOUT') or '45'), int(first('MAX_WORKERS') or '3'), int(first('TOP_K') or '8'))
        if not 1 <= result.max_tokens <= 8192 or not 1 <= result.workers <= 8 or not 1 <= result.top_k <= 30:
            raise ValueError('MAX_TOKENS, MAX_WORKERS or TOP_K outside supported limits')
        if not math.isfinite(result.temperature) or not 0 <= result.temperature <= 2 or not math.isfinite(result.timeout) or not 1 <= result.timeout <= 120:
            raise ValueError('Invalid TEMPERATURE or REQUEST_TIMEOUT')
        return result


class ModelClient:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.client = httpx.Client(transport=transport, timeout=settings.timeout,
                                  limits=httpx.Limits(max_connections=settings.workers, max_keepalive_connections=settings.workers))

    def close(self) -> None:
        self.client.close()

    def generate(self, messages: list[dict[str, str]], deadline: float) -> str:
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError('Run time budget exhausted')
            try:
                response = self.client.post(self.settings.base_url + '/chat/completions',
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
            time.sleep(delay)
        raise ProviderError('Provider did not return an answer')
