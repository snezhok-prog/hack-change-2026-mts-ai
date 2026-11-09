import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from consultant import Consultant
from retrieval import Chunk, Hit, HybridIndex, SearchIndex


class ContextExpansionTests(unittest.TestCase):
    def test_preserves_baseline_and_complete_guards_while_adding_whole_section(self):
        base = Chunk('a.md', 'Alpha / Overview', 'signal overview')
        extra = Chunk('a.md', 'Alpha / Details', 'signal detail with a binding condition at the end.')
        guard = Chunk('a.md', 'Alpha / Чего утверждать нельзя', 'Never promise an unconditional result.')
        core = Consultant(SearchIndex([base, extra, guard]), None)
        original, sources = core.context([Hit(base, 1)])
        expanded, expanded_sources = core.context([Hit(base, 1)], query='signal')
        self.assertTrue(expanded.startswith(original))
        self.assertEqual(expanded_sources[:len(sources)], sources)
        self.assertIn(extra.text, expanded)
        self.assertEqual(expanded.count(guard.text), 1)
        self.assertEqual(expanded_sources[-1]['text'], extra.text)

    def test_skips_oversized_section_and_continues_to_smaller_relevant_section(self):
        base = Chunk('a.md', 'Alpha / Overview', 'signal overview')
        huge = Chunk('a.md', 'Alpha / Large', 'signal ' * 1000)
        small = Chunk('a.md', 'Alpha / Small', 'signal useful detail.')
        index = SearchIndex([base, huge, small])
        ranked = [hit.chunk for hit in index.search('signal', 3)]
        self.assertLess(ranked.index(huge), ranked.index(small))
        expanded, sources = Consultant(index, None).context([Hit(base, 1)], query='signal')
        self.assertNotIn(huge.text, expanded)
        self.assertIn(small.text, expanded)
        self.assertEqual(len(sources), 2)

    def test_counts_formatting_in_total_budget_and_keeps_whole_chunks(self):
        base = Chunk('a.md', 'Alpha / Overview', 'signal overview')
        extra = Chunk('a.md', 'Alpha / Additional', 'signal more facts.')
        core = Consultant(SearchIndex([base, extra]), None)
        original, sources = core.context([Hit(base, 1)])
        exact_size = len(original) + len(f'\n\n### {extra.heading}\n{extra.text}')
        with patch('consultant.CONTEXT_LIMIT', exact_size - 1):
            self.assertEqual(core.context([Hit(base, 1)], query='signal'), (original, sources))
        with patch('consultant.CONTEXT_LIMIT', exact_size):
            expanded, _ = core.context([Hit(base, 1)], query='signal')
        self.assertEqual(len(expanded), exact_size)

    def test_caps_added_material_at_4000_characters(self):
        base = Chunk('a.md', 'Alpha / Overview', 'signal overview')
        extras = [Chunk('a.md', f'Alpha / Detail {i}', 'signal ' * 350) for i in range(3)]
        core = Consultant(SearchIndex([base, *extras]), None)
        original, _ = core.context([Hit(base, 1)])
        expanded, sources = core.context([Hit(base, 1)], query='signal')
        self.assertGreater(len(expanded), len(original))
        self.assertLessEqual(len(expanded) - len(original), 4000)
        self.assertEqual(len(sources), 2)

    def test_adds_nothing_when_baseline_with_guards_already_exceeds_total_budget(self):
        base = Chunk('a.md', 'Alpha / Overview', 'signal overview')
        guard = Chunk('a.md', 'Alpha / Чего утверждать нельзя', 'Binding condition. ' * 1000)
        extra = Chunk('a.md', 'Alpha / Details', 'signal detail')
        core = Consultant(SearchIndex([base, guard, extra]), None)
        original, sources = core.context([Hit(base, 1)])
        self.assertGreater(len(original), 14000)
        self.assertEqual(core.context([Hit(base, 1)], query='signal'), (original, sources))

    def test_expands_only_a_document_already_present_and_is_deterministic(self):
        base = Chunk('a.md', 'Alpha / Overview', 'signal overview')
        extra = Chunk('a.md', 'Alpha / Detail', 'signal relevant detail')
        foreign = [Chunk('b.md', f'Beta / Detail {i}', 'signal signal signal') for i in range(3)]
        chunks = [base, extra, *foreign]
        first = Consultant(SearchIndex(chunks), None).context([Hit(base, 1)], query='signal')
        second = Consultant(SearchIndex(list(reversed(chunks))), None).context([Hit(base, 1)], query='signal')
        self.assertEqual(first, second)
        self.assertIn(extra.text, first[0])
        self.assertEqual({source['document'] for source in first[1]}, {'a.md'})

    def test_answer_supplies_expansion_to_generation_with_hybrid_fallback(self):
        chunks = [Chunk('a.md', 'Alpha / Overview', 'signal signal overview'),
                  Chunk('a.md', 'Alpha / Detail', 'signal complete detail')]
        captured = []
        client = SimpleNamespace(settings=SimpleNamespace(top_k=1),
                                 generate=lambda messages, deadline: captured.extend(messages) or 'Synthetic reply.')
        core = Consultant(HybridIndex(SearchIndex(chunks)), client)
        core.answer('signal', time.monotonic() + 1)
        for chunk in chunks:
            self.assertIn(chunk.text, captured[-1]['content'])


if __name__ == '__main__':
    unittest.main()
