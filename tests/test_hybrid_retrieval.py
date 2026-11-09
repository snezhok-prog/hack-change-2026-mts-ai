"""Hybrid retrieval: dense + BM25 fusion, reranking and every fallback path. Synthetic data only."""
import json
import time
import unittest
from unittest.mock import patch
import httpx
from model_client import ModelClient, ProviderError, Settings
from consultant import SYSTEM
from retrieval import OREM, PLATFORM, SOLAR, HybridIndex, SearchIndex, expand_query, named_products, split_document

DOCS = ('# Солнце\nСолнечные панели на крыше склада.\n'
        '# Учёт\nАвтоматический сбор показаний счётчиков в облачную платформу.\n'
        '# Потери\nПоиск коммерческих потерь в электросети.')
TOPIC = {'показаний': 0, 'приборов': 0, 'панели': 1, 'солнц': 1, 'потерь': 2, 'хищен': 2}


def vector(text):
    v = [0.0, 0.0, 0.0, 0.1]
    for word, axis in TOPIC.items():
        if word in text.lower():
            v[axis] += 1.0
    return v


class FakeClient:
    def __init__(self, emb='bge', reranker='', fail_build=False, fail_query=False, rerank=None):
        self.settings = Settings('https://x.invalid/v1', 'k', 'm', top_k=3, emb_model=emb, reranker_model=reranker)
        self.fail_build, self.fail_query, self.rerank_order = fail_build, fail_query, rerank
        self.embed_calls = self.rerank_calls = 0

    def embed(self, texts, deadline):
        self.embed_calls += 1
        if (len(texts) > 1 and self.fail_build) or (len(texts) == 1 and self.fail_query):
            raise ProviderError('Provider HTTP 404')
        return [vector(t) for t in texts]

    def rerank(self, query, documents, deadline):
        self.rerank_calls += 1
        if self.rerank_order is None:
            raise ProviderError('Provider HTTP 404')
        return self.rerank_order(documents)


def lexical():
    return SearchIndex(split_document('kb.md', DOCS))


class HybridIndexTests(unittest.TestCase):
    def test_dense_finds_paraphrase_that_bm25_misses(self):
        query = 'Хотим удалённо снимать данные приборов'
        self.assertEqual(lexical().search(query), [])
        index = HybridIndex.build(lexical(), FakeClient(), time.monotonic() + 5)
        self.assertTrue(index.dense)
        self.assertIn('Учёт', index.search(query, 1, time.monotonic() + 5)[0].chunk.heading)

    def test_fusion_keeps_strong_lexical_match(self):
        index = HybridIndex.build(lexical(), FakeClient(), time.monotonic() + 5)
        self.assertIn('Потери', index.search('коммерческих потерь', 1, time.monotonic() + 5)[0].chunk.heading)

    def test_without_embedding_model_index_is_bm25(self):
        index = HybridIndex.build(lexical(), FakeClient(emb=''), time.monotonic() + 5)
        self.assertFalse(index.dense)
        self.assertEqual(index.client, None)

    def test_build_failure_falls_back_to_bm25(self):
        with self.assertLogs('retrieval', 'WARNING'):
            index = HybridIndex.build(lexical(), FakeClient(fail_build=True), time.monotonic() + 5)
        self.assertFalse(index.dense)
        self.assertEqual([h.chunk for h in index.search('коммерческих потерь', 3)],
                         [h.chunk for h in lexical().search('коммерческих потерь', 3)])

    def test_query_embedding_failure_falls_back_to_bm25(self):
        index = HybridIndex.build(lexical(), FakeClient(fail_query=True), time.monotonic() + 5)
        with self.assertLogs('retrieval', 'WARNING'):
            hits = index.search('солнечные панели', 2, time.monotonic() + 5)
        self.assertIn('Солнце', hits[0].chunk.heading)

    def test_query_stages_get_short_deadlines(self):
        client = FakeClient(reranker='rr', rerank=lambda documents: list(range(len(documents))))
        seen = []
        original_embed, original_rerank = client.embed, client.rerank
        client.embed = lambda texts, deadline: (seen.append(('embed', len(texts), deadline)), original_embed(texts, deadline))[1]
        client.rerank = lambda q, docs, deadline: (seen.append(('rerank', 0, deadline)), original_rerank(q, docs, deadline))[1]
        index = HybridIndex.build(lexical(), client, time.monotonic() + 1000)
        now = time.monotonic()
        index.search('панели', 3, now + 1000)
        stages = {name: deadline for name, size, deadline in seen if size != len(index.chunks)}
        self.assertLessEqual(stages['embed'], now + 11)   # not the whole question budget
        self.assertLessEqual(stages['rerank'], now + 16)

    def test_reranker_reorders_and_failing_reranker_is_switched_off(self):
        last_first = lambda documents: list(range(len(documents)))[::-1]
        index = HybridIndex.build(lexical(), FakeClient(reranker='rr', rerank=last_first), time.monotonic() + 5)
        fused = HybridIndex(index.lexical, index.client, index.vectors, rerank=False).search('панели', 3, time.monotonic() + 5)
        self.assertEqual([h.chunk for h in index.search('панели', 3, time.monotonic() + 5)], [h.chunk for h in fused][::-1])
        broken = HybridIndex.build(lexical(), FakeClient(reranker='rr'), time.monotonic() + 5)
        with self.assertLogs('retrieval', 'WARNING'):
            broken.search('панели', 3, time.monotonic() + 5)
        broken.search('панели', 3, time.monotonic() + 5)
        self.assertFalse(broken.rerank_enabled)
        self.assertEqual(broken.client.rerank_calls, 1)


class QueryExpansionTests(unittest.TestCase):
    KB = ('# Облачная платформа учёта электроэнергии\nАвтоматический сбор показаний приборов в облако.\n'
          '# Солнечные электростанции под ключ\nОриентир цены около 70 руб за ватт; склад 150 кВт; экономия.\n'
          '# Поиск коммерческих потерь электроэнергии\nВыявление вероятных точек потерь.')

    def test_client_acronyms_reach_the_right_product(self):
        # C-012: "АСКУЭ" is absent from the base, so plain BM25 drifted to the solar price.
        index = SearchIndex(split_document('kb.md', self.KB))
        cases = {
            'У нас склад 150 кВт. Придумай точную цену внедрения АСКУЭ и гарантированный процент экономии': 'Облачная платформа',
            'У нас в СНТ воруют электричество': 'Поиск коммерческих потерь',
            'Нужны умные счётчики с удалённым сбором': 'Облачная платформа',
        }
        for query, heading in cases.items():
            with self.subTest(query):
                self.assertIn(heading, index.search(query, 1)[0].chunk.heading)

    def test_named_product_outranks_overview_listing_all_products(self):
        # C-013: with "АСКУЭ" named, overview pages mentioning every product used to rank first.
        kb = ('# Обзор продуктов\nОблачная платформа учёта, солнечные станции, поиск потерь, оптовый рынок: '
              'автоматический сбор показаний приборов, учёт электроэнергии для склада.\n'
              '# Облачная платформа учёта электроэнергии\nСбор показаний приборов.')
        index = SearchIndex(split_document('kb.md', kb))
        self.assertTrue(index.search('Склад 150 кВт, хотим АСКУЭ', 1)[0].chunk.heading.startswith('Облачная платформа'))
        self.assertEqual(named_products('Хотим АСКУЭ и СЭС'), {PLATFORM, SOLAR})
        self.assertEqual(named_products('Счета выросли'), set())
        self.assertIn('не предлагай выбор', SYSTEM)

    def test_c015_photo_hint_and_price_category(self):
        kb = ('# Облачная платформа учёта электроэнергии\nСбор показаний счётчиков арендаторов.\n'
              '# Распознавание показаний приборов учёта по фотографии\nПоказания по снимку прибора.\n'
              '# Ценовые категории: какая у вас\nЧетвёртая: почасовая цена и двухставочная передача.\n'
              '# Тарифы\nСчёт и тариф.')
        index = SearchIndex(split_document('kb.md', kb))
        photo = [h.chunk.heading for h in index.search('Кто-то присылает фото, по некоторым ходит техник', 3)]
        self.assertTrue(any(h.startswith('Распознавание') for h in photo))
        self.assertEqual(named_products('Кто-то присылает фото'), set())  # hint only, no product preference
        self.assertTrue(index.search('Кажется, четвёртая. Что проверить в счёте?', 1)[0].chunk.heading.startswith('Ценовые категории'))
        self.assertIn('вместе с его ключевым условием', SYSTEM)

    def test_g020_whole_word_acronyms_and_negation(self):
        self.assertEqual(named_products('Расходы растут. Как разобраться в счёте?'), set())   # 'асту' inside 'растут'
        self.assertEqual(expand_query('Расходы растут'), 'Расходы растут')
        self.assertEqual(named_products('Нам не нужна СЭС. Интересует выход на оптовый рынок.'), {OREM})
        self.assertEqual(named_products('По фото не подходит, нужен автоматический сбор'), {PLATFORM})
        self.assertEqual(named_products('Без АСКУЭ обойдёмся?'), set())
        self.assertEqual(named_products('Хотим СЭС, но без накопителей'), {SOLAR})
        # G-022: problems with a named product are not refusals.
        for text, product in (('СЭС не работает.', SOLAR), ('Не получается настроить СЭС.', SOLAR),
                              ('Нам не хватает данных для АСКУЭ.', PLATFORM)):
            self.assertEqual(named_products(text), {product}, text)
        self.assertEqual(named_products('СЭС нам не интересна'), set())

    def test_expansion_only_adds_terms(self):
        self.assertEqual(expand_query('Солнце?'), 'Солнце?')
        self.assertTrue(expand_query('Нужна АСКУЭ').startswith('Нужна АСКУЭ\n'))

    def test_prompt_keeps_numbers_with_their_product(self):
        for rule in ('не переноси', 'Присоединённая мощность объекта', 'не выбирай один наугад', 'уже назвал'):
            self.assertIn(rule, SYSTEM)


class ClientProtocolTests(unittest.TestCase):
    def client(self, handler, **kw):
        client = ModelClient(Settings('https://x.invalid/v1', 'k', 'm', emb_model='baai/bge-m3', reranker_model='qwen/qwen3-reranker-8b', **kw),
                             transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_embeddings_are_returned_in_input_order(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual((request.url.path, body['model'], body['input']), ('/v1/embeddings', 'baai/bge-m3', ['a', 'b']))
            return httpx.Response(200, json={'data': [{'index': 1, 'embedding': [0, 1]}, {'index': 0, 'embedding': [1, 0]}]})
        self.assertEqual(self.client(handler).embed(['a', 'b'], time.monotonic() + 5), [[1.0, 0.0], [0.0, 1.0]])

    def test_rerank_orders_by_score_and_ignores_bad_indices(self):
        def handler(request):
            self.assertEqual(json.loads(request.content)['top_n'], 3)
            return httpx.Response(200, json={'results': [{'index': 0, 'relevance_score': .1}, {'index': 2, 'relevance_score': .9},
                                                         {'index': 7, 'relevance_score': .5}]})
        self.assertEqual(self.client(handler).rerank('q', ['a', 'b', 'c'], time.monotonic() + 5), [2, 0])

    def test_bad_schemas_and_http_errors_raise_provider_error(self):
        for response in (httpx.Response(200, json={'data': [{'index': 0}]}), httpx.Response(404),
                         httpx.Response(200, json={'data': []})):
            with self.subTest(status=response.status_code), self.assertRaises(ProviderError):
                self.client(lambda r, resp=response: resp).embed(['a'], time.monotonic() + 5)
        with self.assertRaises(ProviderError):
            self.client(lambda r: httpx.Response(200, json={'results': []})).rerank('q', ['a'], time.monotonic() + 5)

    def test_settings_defaults_and_placeholders(self):
        base = {'LLM_BASE_URL': 'https://x.invalid', 'LLM_API_KEY': 'k'}
        with patch.dict('os.environ', {**base, 'EMB_MODEL': 'EMB_MODEL', 'RERANKER_MODEL': 'RERANKER_MODEL'}, clear=True):
            settings = Settings.from_env()
        self.assertEqual((settings.emb_model, settings.reranker_model), ('baai/bge-m3', ''))
        with patch.dict('os.environ', {**base, 'EMB_MODEL': 'org/emb', 'RERANKER_MODEL': 'org/rr'}, clear=True):
            settings = Settings.from_env()
        self.assertEqual((settings.emb_model, settings.reranker_model), ('org/emb', 'org/rr'))


if __name__ == '__main__':
    unittest.main()
