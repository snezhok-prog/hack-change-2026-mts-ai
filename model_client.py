"""Organizer-compatible chat API with explicit configuration and bounded retries."""
from dataclasses import dataclass, field
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import math
import os
import re
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
    emb_model: str = ''        # empty: dense retrieval off
    reranker_model: str = ''   # empty: no reranking

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
                   env_number('TOP_K', 8, 1, 30, int),
                   env_first('EMB_MODEL') or DEFAULT_EMB_MODEL,
                   env_first('RERANKER_MODEL'))


DEFAULT_MODEL = 'qwen/qwen3.8-27b'
DEFAULT_EMB_MODEL = 'baai/bge-m3'
MIN_ANSWER_TOKENS = 1024
MAX_GROWN_TOKENS = 4096
NO_THINK = '/no_think'  # Qwen3 in-prompt switch; works even if the proxy drops the request field  # organizer sample default; also served by OpenRouter
THINK_BLOCK = re.compile(r'<think>.*?</think>', re.S | re.I)


def strip_reasoning(text: str) -> str:
    """Remove Qwen3 reasoning. An unterminated <think> means no final answer was produced."""
    text = THINK_BLOCK.sub('', text)
    if '</think>' in text.lower():
        text = text[text.lower().rindex('</think>') + len('</think>'):]
    if text.lstrip().lower().startswith('<think>'):
        return ''
    return text.strip()


def trim_to_sentence(text: str) -> str:
    """Cut a length-truncated answer back to its last complete sentence (if there is one)."""
    ends = [m.end() for m in re.finditer(r'[.!?…](?=\s|$)', text)]
    return text[:ends[-1]].strip() if ends else text.strip()


def reasoning_off_params(base_url: str) -> dict:
    if 'openrouter.ai' in base_url:
        return {'reasoning': {'enabled': False}}
    return {'chat_template_kwargs': {'enable_thinking': False}}


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
        # Ask Qwen3 not to emit reasoning; dropped for good if the proxy rejects the field.
        self.reasoning_off = reasoning_off_params(settings.base_url)

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

    def _run(self, coroutine, deadline: float):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or self.closed:
            coroutine.close()
            raise ProviderError('Run time budget exhausted' if remaining <= 0 else 'Model client is closed')
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=remaining)
        except FutureTimeout:
            future.cancel()
            raise ProviderError('Run time budget exhausted') from None

    def generate(self, messages: list[dict[str, str]], deadline: float) -> str:
        return self._run(self._generate(messages, deadline), deadline)

    def embed(self, texts: list[str], deadline: float) -> list[list[float]]:
        """POST /embeddings; vectors returned in input order."""
        data = self._run(self._post_json('/embeddings', {'model': self.settings.emb_model, 'input': texts}, deadline), deadline)
        try:
            items = sorted(data['data'], key=lambda item: int(item.get('index', 0)))
            vectors = [[float(x) for x in item['embedding']] for item in items]
        except (KeyError, TypeError, ValueError):
            raise ProviderError('Invalid embeddings response schema') from None
        if len(vectors) != len(texts) or not vectors or len({len(v) for v in vectors}) != 1:
            raise ProviderError('Embeddings response does not match the input')
        return vectors

    def rerank(self, query: str, documents: list[str], deadline: float) -> list[int]:
        """POST /rerank; document indices from most to least relevant."""
        data = self._run(self._post_json('/rerank', {'model': self.settings.reranker_model, 'query': query,
                                                     'documents': documents, 'top_n': len(documents)}, deadline), deadline)
        try:
            entries = data.get('results', data.get('data', []))
            order = [int(e['index']) for e in sorted(entries, key=lambda e: -float(e.get('relevance_score', e.get('score', 0))))]
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ProviderError('Invalid rerank response schema') from None
        order = [i for i in dict.fromkeys(order) if 0 <= i < len(documents)]
        if not order:
            raise ProviderError('Empty rerank response')
        return order

    async def _post_json(self, path: str, payload: dict, deadline: float) -> dict:
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError('Run time budget exhausted')
            try:
                response = await self.client.post(self.settings.base_url + path,
                    headers={'Authorization': 'Bearer ' + self.settings.api_key}, json=payload,
                    timeout=httpx.Timeout(min(self.settings.timeout, remaining), connect=min(10, remaining)))
            except httpx.TransportError:
                if attempt == 1:
                    raise ProviderError('Provider transport failure') from None
            else:
                if response.is_success:
                    try:
                        return response.json()
                    except ValueError:
                        raise ProviderError('Invalid provider response') from None
                if response.status_code not in {408, 429, 500, 502, 503, 504} or attempt == 1:
                    raise ProviderError(f'Provider HTTP {response.status_code}')
            await asyncio.sleep(min(.5, max(0, deadline - time.monotonic())))
        raise ProviderError('Provider did not return an answer')

    async def _generate(self, messages: list[dict[str, str]], deadline: float) -> str:
        # Qwen3 may ignore the reasoning switch on some proxies and spend the whole budget
        # inside <think>; keep a floor for the answer and grow it once if reasoning was cut off.
        max_tokens = max(self.settings.max_tokens, MIN_ANSWER_TOKENS)
        grown = False
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError('Run time budget exhausted')
            try:
                extra = self.reasoning_off
                response = await self.client.post(self.settings.base_url + '/chat/completions',
                    headers={'Authorization': 'Bearer ' + self.settings.api_key},
                    json={'model': self.settings.model, 'messages': messages,
                          'temperature': self.settings.temperature, 'max_tokens': max_tokens, **extra},
                    timeout=httpx.Timeout(min(self.settings.timeout, remaining), connect=min(10, remaining)))
            except httpx.TransportError:
                if attempt == 2:
                    raise ProviderError('Provider transport failure') from None
            else:
                if response.status_code in {400, 422} and extra:
                    self.reasoning_off = {}  # proxy does not accept the field; retry plain at once
                    continue
                if response.status_code not in {408, 429, 500, 502, 503, 504}:
                    if not response.is_success:
                        raise ProviderError(f'Provider HTTP {response.status_code}')
                    try:
                        choice = response.json()['choices'][0]
                        raw, finish = choice['message']['content'], choice.get('finish_reason')
                    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                        raise ProviderError('Invalid provider response schema') from None
                    answer = strip_reasoning(raw) if isinstance(raw, str) else raw
                    if not isinstance(answer, str) or not answer.strip():
                        if isinstance(raw, str) and '<think' in raw.lower() and not grown:
                            grown = True  # reasoning hit the token limit: one retry with a larger budget
                            max_tokens = min(MAX_GROWN_TOKENS, max_tokens * 2)
                            continue
                        raise ProviderError('Provider returned an empty answer')
                    if finish == 'length':
                        if not grown:
                            grown = True  # answer cut by the token limit: one retry with a larger budget
                            max_tokens = min(MAX_GROWN_TOKENS, max_tokens * 2)
                            continue
                        answer = trim_to_sentence(answer)  # never hand a half sentence to the client
                    if len(answer.strip()) > 10000:
                        raise ProviderError('Provider answer exceeds 10000 characters')
                    return answer.strip()
                if attempt == 2:
                    raise ProviderError(f'Provider HTTP {response.status_code} after retries')
            delay = min(.5 * (2 ** attempt), max(0, deadline - time.monotonic()))
            await asyncio.sleep(delay)
        raise ProviderError('Provider did not return an answer')
