import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile
from retrieval import SearchIndex, dialogue_query, load_archive, split_document


class RetrievalTests(unittest.TestCase):
    def test_finds_material_far_beyond_document_start(self):
        text = '# Каталог\n' + 'Введение. ' * 100 + '\n## Особенность\nДатчики поддерживают спутниковую связь.'
        hits = SearchIndex(split_document('catalog.md', text)).search('Спутниковые датчики')
        self.assertIn('спутниковую', hits[0].chunk.text)
        self.assertIn('Особенность', hits[0].chunk.heading)

    def test_unknown_query_has_no_irrelevant_hits(self):
        self.assertEqual(SearchIndex(split_document('a.md', '# Учёт\nДатчики расхода')).search('рецепт борща'), [])

    def test_long_section_tail_is_preserved(self):
        text = 'начало ' * 900 + '\nуникальныйхвост'
        chunks = split_document('a.md', text)
        self.assertTrue(any('уникальныйхвост' in c.text for c in chunks))
        self.assertTrue(all(len(c.text) <= 2800 for c in chunks))

    def test_history_uses_user_details_not_assistant_claims(self):
        query = dialogue_query('Пользователь: У нас склад. Ассистент: Гарантируем экономию. Пользователь: А вечером?')
        self.assertIn('склад', query)
        self.assertIn('вечером', query)
        self.assertNotIn('Гарантируем', query)

    def test_archive_read_and_unsafe_path_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'kb.zip'
            with ZipFile(path, 'w') as archive:
                archive.writestr('products/info.md', '# Тест\nПример')
            self.assertEqual(load_archive(str(path))[0].source, 'products/info.md')
            with ZipFile(path, 'w') as archive:
                archive.writestr('../escape.md', 'bad')
            with self.assertRaises(ValueError):
                load_archive(str(path))


if __name__ == '__main__':
    unittest.main()
