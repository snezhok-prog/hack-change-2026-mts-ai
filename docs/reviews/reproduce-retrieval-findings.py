import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
"""Synthetic probes only; no network, API credentials or organizer materials."""
import json
from retrieval import Chunk, Hit, SearchIndex, named_products
from consultant import Consultant
probes=['Расходы растут. Как разобраться в счёте?', 'Нам не нужна СЭС. Интересует выход на оптовый рынок.', 'Распознавание по фото не подходит. Нужен автоматический сбор.']
for query in probes:
 print(json.dumps({'query':query,'named_products':sorted(named_products(query))},ensure_ascii=False))
condition='Обязательное условие: только при уже установленных совместимых приборах.'
text=('Синтетическое защитное правило. '*85)+condition
chunks=[Chunk('synthetic.md','Облачная платформа учёта / Возможности','Синтетическая услуга.'),Chunk('synthetic.md','Облачная платформа учёта / Чего утверждать нельзя',text)]
core=Consultant(SearchIndex(chunks),None)
material,sources=core.context([Hit(chunks[0],1)])
print(json.dumps({'guard_chars':len(text),'condition_in_model_context':condition in material,'condition_in_debug_sources':any(condition in s['text'] for s in sources),'source_chars':len(sources[-1]['text'])},ensure_ascii=False))
