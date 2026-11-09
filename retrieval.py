"""Local lexical retrieval over complete Markdown sections; no model downloads."""
from collections import Counter
from dataclasses import dataclass
import logging
import math
from pathlib import PurePosixPath
import re
import time
from zipfile import ZipFile

LOG = logging.getLogger('retrieval')
# Client wording -> knowledge-base wording. The base names products in full ("облачная
# платформа учёта") while clients use acronyms and colloquialisms; BM25 cannot bridge that.
# Expansions add search terms only; they never become product facts.
PLATFORM, OREM, SOLAR, LOSSES, PHOTO = ('облачная платформа учета', 'выход на оптовый рынок',
                                       'солнечные электростанции', 'поиск коммерческих потерь', 'распознавание показаний')
# (client wording, search terms to add, product heading prefix to prefer)
QUERY_SYNONYMS = (
    # Acronyms only as whole words: 'асту' must not fire inside 'растут' (G-020).
    (r'\bаскуэ\b|\bасту\b|\bаиис\b|автоматизированн\w* систем\w* учет|автоматическ\w* (?:учет|сбор)|автоматизир\w* (?:учет|сбор)|удаленн\w* (?:сбор|сня)', 'облачная платформа учёта электроэнергии автоматический сбор показаний приборов', PLATFORM),
    (r'умн\w* сч[её]тчик|смарт.?сч[её]тчик|интеллектуальн\w* сч[её]тчик', 'интеллектуальная система учёта облачная платформа учёта', PLATFORM),
    (r'\bорэм\b|оптов\w* рын', 'выход на оптовый рынок электроэнергии', OREM),
    (r'\bсэс\b|солнечн\w* (?:панел|станц|электростанц)|фотоэлектр', 'солнечная электростанция', SOLAR),
    (r'хищени|воруют|ворую|\bкраж|безучетн|неучтенн|незаконн\w* подключ', 'поиск коммерческих потерь электроэнергии', LOSSES),
    (r'по фото|фотограф|сфотографир|\bфотк|снимок сч[её]тчик', 'распознавание показаний приборов учёта по фотографии', PHOTO),
    (r'сим.?карт|\bsim\b|\bсимк', 'передача данных приборов учёта облачная платформа', PLATFORM),
    # Topic hints without a product preference: the client mentions it in passing.
    (r'\bфото\b|\bфотки?\b|присыла\w* фото', 'распознавание показаний приборов учёта по фотографии', None),
    (r'(?:перв|втор|трет|четв[её]рт|пят|шест)\w* (?:ценов\w* )?категор|ценов\w* категор|\bчетв[её]ртая\b|\bшестая\b',
     'ценовые категории какая выгоднее почасовая цена двухставочная передача', None),  # expansion only (C-020)
)
# Only explicit refusals rule a product out (G-022): 'СЭС не работает' or 'не хватает данных
# для АСКУЭ' still name the product.
REFUSAL = r'(?:не\s+(?:нужн|подход|интерес|рассматрива|планиру|хотим|хочу)\w*|откаж\w*|откаже\w*)'
NEGATION_BEFORE = re.compile(REFUSAL + r'\s+(?:\w+\s+){0,2}$|\bбез\s+(?:\w+\s+)?$')
NEGATION_AFTER = re.compile(r'\s*(?:\w+\s+)?(?:' + REFUSAL + r'|обойд[её]мся)')
NAMED_PRODUCT_BOOST = 1.6  # a product the client named outranks overview pages listing every product
STOP = set('и в во на с со а но или для по из у к от до за о об это как что мы вы он она они не да ли бы же то уже есть нас вас мне нам ваш наш даже если этих эти этот хотим хочу'.split())


def _negated(lowered: str, start: int, end: int) -> bool:
    """'не нужна СЭС', 'без АСКУЭ', 'по фото не подходит': the client rules the topic out."""
    before = re.split(r'[.!?;,]', lowered[max(0, start - 25):start])[-1]
    after = re.split(r'[.!?;,]', lowered[end:end + 25])[0]
    return bool(NEGATION_BEFORE.search(before) or NEGATION_AFTER.match(after))


def _matches(text: str):
    lowered = text.lower().replace('ё', 'е')
    found = []
    for pattern, phrase, product in QUERY_SYNONYMS:
        hits = list(re.finditer(pattern.replace('ё', 'е'), lowered))
        if hits and not all(_negated(lowered, m.start(), m.end()) for m in hits):
            found.append((phrase, product))
    return found


def expand_query(text: str) -> str:
    extra = [phrase for phrase, _ in _matches(text)]
    return text if not extra else text + '\n' + '\n'.join(extra)


def named_products(text: str) -> set[str]:
    """Product heading prefixes the client referred to (lowercase, ё→е)."""
    return {product for _, product in _matches(text) if product}


def tokens(text: str) -> list[str]:
    words = re.findall(r'[a-zа-я0-9]+', text.lower().replace('ё', 'е'))
    # Prefix terms add Russian inflection tolerance without a downloaded NLP model.
    return [w[:5] if re.fullmatch('[а-я]{6,}', w) else w for w in words if w not in STOP and len(w) > 1]


@dataclass(frozen=True)
class Chunk:
    source: str
    heading: str
    text: str


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


def split_document(source: str, text: str, max_chars: int = 2800) -> list[Chunk]:
    """Keep ordinary tables with their headings; overlap unusually long sections."""
    if max_chars < 200:
        raise ValueError('Chunk size must be at least 200 characters')
    chunks: list[Chunk] = []
    headings: list[str] = []
    body: list[str] = []

    def flush() -> None:
        section = '\n'.join(body).strip()
        if not section:
            return
        start = 0
        while start < len(section):
            end = min(start + max_chars, len(section))
            if end < len(section):
                boundary = section.rfind('\n', start + max_chars // 2, end)
                if boundary > start:
                    end = boundary
            chunks.append(Chunk(source, ' / '.join(headings), section[start:end]))
            if end == len(section):
                break
            start = max(start + 1, end - min(350, max_chars // 4))

    for line in text.splitlines():
        match = re.match(r'^(#{1,6})\s+(.+)', line)
        if match:
            flush()
            body.clear()
            level = len(match[1])
            headings[level - 1:] = [match[2].strip()]
        else:
            body.append(line)
    flush()
    return chunks


def load_archive(path: str) -> list[Chunk]:
    result: list[Chunk] = []
    with ZipFile(path) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        if len(members) > 5000 or sum(m.file_size for m in members) > 32 * 1024 * 1024:
            raise ValueError('Knowledge archive exceeds safe limits')
        for member in sorted(members, key=lambda m: m.filename):
            name = PurePosixPath(member.filename)
            if name.is_absolute() or '..' in name.parts or '\\' in member.filename:
                raise ValueError('Unsafe archive member path')
            if '__MACOSX' in name.parts or name.suffix.lower() not in {'.md', '.txt'}:
                continue
            content = archive.read(member).decode('utf-8-sig')
            result.extend(split_document(str(name), content))
    if not result:
        raise ValueError('Knowledge archive has no readable text sections')
    return result


class SearchIndex:
    def __init__(self, chunks: list[Chunk]):
        if not chunks:
            raise ValueError('Cannot index empty knowledge base')
        self.chunks = tuple(chunks)
        self.terms = [Counter(tokens(c.heading + '\n' + c.text)) for c in chunks]
        self.lengths = [sum(t.values()) for t in self.terms]
        self.average = sum(self.lengths) / len(chunks) or 1
        counts = Counter(term for document in self.terms for term in document)
        self.idf = {term: math.log(1 + (len(chunks) - count + .5) / (count + .5)) for term, count in counts.items()}

    def search(self, query: str, limit: int = 8) -> list[Hit]:
        if limit < 1:
            raise ValueError('Search limit must be positive')
        terms = set(tokens(expand_query(query)))
        products = named_products(query)
        hits = []
        for chunk, counts, size in zip(self.chunks, self.terms, self.lengths):
            norm = 1.2 * (.25 + .75 * size / self.average)
            score = sum(self.idf[t] * counts[t] * 2.2 / (counts[t] + norm) for t in terms if counts[t])
            if products and score > 0 and chunk.heading.lower().replace('ё', 'е').startswith(tuple(products)):
                score *= NAMED_PRODUCT_BOOST
            if score > 0:
                hits.append(Hit(chunk, score))
        return sorted(hits, key=lambda hit: (-hit.score, hit.chunk.source, hit.chunk.heading))[:limit]


def dialogue_query(text: str) -> str:
    """Use user turns for retrieval; assistant claims are not product evidence."""
    turns = re.split(r'(?:^|\n|\s)(Пользователь|Ассистент|User|Assistant):\s*', text, flags=re.I)
    users = [turns[i + 1].strip() for i in range(1, len(turns) - 1, 2) if turns[i].lower() in {'пользователь', 'user'}]
    return '\n'.join(users[-3:]) if users else text


CANDIDATES = 30       # per retriever, before fusion
RRF_K = 60            # standard Reciprocal Rank Fusion constant
RERANK_POOL = 16      # fused candidates sent to the reranker
EMBED_BATCH = 32
QUERY_EMBED_SECONDS = 10.0
RERANK_SECONDS = 15.0


def embed_text(chunk: Chunk) -> str:
    return f'{chunk.heading}\n{chunk.text}'[:4000]


def normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


class HybridIndex:
    """BM25 + dense (bge-m3) fused with RRF, optionally reranked.

    Every remote step is best-effort: if embeddings are unavailable at build time the
    index is plain BM25; if a query embedding or the reranker fails, that query falls back
    to the previous stage. A failing reranker is switched off to save the time budget.
    """

    def __init__(self, lexical: SearchIndex, client=None, vectors: list[list[float]] | None = None, rerank: bool = False):
        self.lexical, self.client, self.vectors, self.rerank_enabled = lexical, client, vectors, rerank
        self.chunks = lexical.chunks

    @classmethod
    def build(cls, lexical: SearchIndex, client, deadline: float) -> 'HybridIndex':
        settings = getattr(client, 'settings', None)
        if settings is None or not getattr(settings, 'emb_model', ''):
            return cls(lexical)
        texts = [embed_text(c) for c in lexical.chunks]
        try:
            vectors: list[list[float]] = []
            for start in range(0, len(texts), EMBED_BATCH):
                vectors.extend(client.embed(texts[start:start + EMBED_BATCH], deadline))
        except Exception as error:  # any provider/schema problem: keep the lexical index
            LOG.warning('Dense retrieval disabled (%s)', type(error).__name__)
            return cls(lexical)
        return cls(lexical, client, [normalize(v) for v in vectors], bool(getattr(settings, 'reranker_model', '')))

    @property
    def dense(self) -> bool:
        return self.vectors is not None

    def search(self, query: str, limit: int = 8, deadline: float | None = None) -> list[Hit]:
        lexical = self.lexical.search(query, CANDIDATES)
        if not self.dense or not query.strip():
            return lexical[:limit]
        deadline = deadline if deadline is not None else time.monotonic() + 30
        # Optional stages get short deadlines so a slow embedder/reranker cannot starve generation.
        try:
            q = normalize(self.client.embed([query], min(deadline, time.monotonic() + QUERY_EMBED_SECONDS))[0])
        except Exception as error:
            LOG.warning('Query embedding failed (%s); using BM25', type(error).__name__)
            return lexical[:limit]
        similarity = [sum(a * b for a, b in zip(q, v)) for v in self.vectors]
        dense = sorted(range(len(self.chunks)), key=lambda i: -similarity[i])[:CANDIDATES]
        position = {id(chunk): i for i, chunk in enumerate(self.chunks)}
        fused: dict[int, float] = {}
        for rank, hit in enumerate(lexical):
            i = position[id(hit.chunk)]
            fused[i] = fused.get(i, 0.0) + 1 / (RRF_K + rank + 1)
        for rank, i in enumerate(dense):
            fused[i] = fused.get(i, 0.0) + 1 / (RRF_K + rank + 1)
        order = sorted(fused, key=lambda i: (-fused[i], i))
        if self.rerank_enabled:
            pool = order[:RERANK_POOL]
            try:
                ranked = self.client.rerank(query, [embed_text(self.chunks[i])[:2000] for i in pool],
                                            min(deadline, time.monotonic() + RERANK_SECONDS))
                order = [pool[r] for r in ranked] + [i for i in pool if i not in {pool[r] for r in ranked}] + order[RERANK_POOL:]
            except Exception as error:
                LOG.warning('Reranker disabled (%s)', type(error).__name__)
                self.rerank_enabled = False
        return [Hit(self.chunks[i], round(fused[i], 6)) for i in order[:limit]]
