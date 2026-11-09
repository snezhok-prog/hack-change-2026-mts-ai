"""Local lexical retrieval over complete Markdown sections; no model downloads."""
from collections import Counter
from dataclasses import dataclass
import math
from pathlib import PurePosixPath
import re
from zipfile import ZipFile

STOP = set('и в во на с со а но или для по из у к от до за о об это как что мы вы он она они не да ли бы же то уже есть нас вас мне нам ваш наш'.split())


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
        terms = set(tokens(query))
        hits = []
        for chunk, counts, size in zip(self.chunks, self.terms, self.lengths):
            norm = 1.2 * (.25 + .75 * size / self.average)
            score = sum(self.idf[t] * counts[t] * 2.2 / (counts[t] + norm) for t in terms if counts[t])
            if score > 0:
                hits.append(Hit(chunk, score))
        return sorted(hits, key=lambda hit: (-hit.score, hit.chunk.source, hit.chunk.heading))[:limit]


def dialogue_query(text: str) -> str:
    """Use user turns for retrieval; assistant claims are not product evidence."""
    turns = re.split(r'(?:^|\n|\s)(Пользователь|Ассистент|User|Assistant):\s*', text, flags=re.I)
    users = [turns[i + 1].strip() for i in range(1, len(turns) - 1, 2) if turns[i].lower() in {'пользователь', 'user'}]
    return '\n'.join(users[-3:]) if users else text
