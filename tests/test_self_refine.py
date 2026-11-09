"""Adaptive self-refine: second pass only with ample budget; the draft survives any failure."""
import time
import unittest
from consultant import REFINE_RESERVE_SECONDS, REFINE_SYSTEM, Consultant
from model_client import ProviderError, Settings
from retrieval import SearchIndex, split_document

KB = '# Платформа\nПлатформа учёта собирает показания приборов автоматически.'


class Client:
    settings = Settings('https://x.invalid/v1', 'k', 'm', top_k=2)

    def __init__(self, refined='Да, платформа собирает показания автоматически. Сколько у вас приборов?', fail=False):
        self.refined, self.fail, self.calls = refined, fail, []

    def generate(self, messages, deadline):
        self.calls.append(messages[0]['content'])
        if messages[0]['content'] == REFINE_SYSTEM:
            if self.fail:
                raise ProviderError('Provider HTTP 503')
            return self.refined
        return 'Черновой ответ про платформу учёта и показания приборов.'


def core(client):
    return Consultant(SearchIndex(split_document('kb.md', KB)), client)


class SelfRefineTests(unittest.TestCase):
    def test_refines_when_run_has_ample_budget(self):
        client = Client()
        result = core(client).answer('Платформа учёта?', time.monotonic() + REFINE_RESERVE_SECONDS + 60)
        self.assertEqual(result.answer, 'Да, платформа собирает показания автоматически. Сколько у вас приборов?')
        self.assertEqual(len(client.calls), 2)

    def test_skips_second_pass_when_budget_is_short(self):
        client = Client()
        result = core(client).answer('Платформа учёта?', time.monotonic() + 30)  # e.g. the web chat
        self.assertEqual(result.answer, 'Черновой ответ про платформу учёта и показания приборов.')
        self.assertEqual(len(client.calls), 1)

    def test_failure_or_implausible_rewrite_keeps_the_draft(self):
        for client in (Client(fail=True), Client(refined='Да.'), Client(refined='Вот исправленный черновик ответа клиенту про платформу.'),
                       Client(refined='слово ' * 130)):
            with self.subTest(refined=client.refined[:20], fail=client.fail), self.assertLogs('consultant', 'WARNING') if client.fail else _quiet():
                result = core(client).answer('Платформа учёта?', time.monotonic() + REFINE_RESERVE_SECONDS + 60)
            self.assertEqual(result.answer, 'Черновой ответ про платформу учёта и показания приборов.')


class _quiet:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


if __name__ == '__main__':
    unittest.main()
