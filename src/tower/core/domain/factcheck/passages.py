"""Passages (pipeline.md E7): chunking, lexical ranking (BM25), selection and
near-duplicate removal by SimHash, in pure Python (no numpy)."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from tower.core.domain.text import hamming, normalize, simhash64

__all__ = [
    "Chunk",
    "RankedChunk",
    "bm25_rank",
    "chunk_text",
    "dedupe_chunks",
    "select_passages",
    "tokenize",
]

TARGET_WORDS = 120


@dataclass(frozen=True, slots=True)
class Chunk:
    source_key: str
    """Identifies the source the chunk comes from (URL or stable id)."""
    position: int
    """Index of the chunk within its source."""
    text: str


@dataclass(frozen=True, slots=True)
class RankedChunk:
    chunk: Chunk
    score: float
    simhash: int


def tokenize(text: str) -> list[str]:
    """Casefolded, accent-folded word tokens (letters and digits), no stop-word list."""
    return [t for t in _WORD.findall(_fold(text)) if len(t) > 1 or t.isdigit()]


def chunk_text(
    source_key: str, text: str, *, target_words: int = TARGET_WORDS, max_chunks: int = 200
) -> list[Chunk]:
    """Split into chunks of about ``target_words`` words, cutting at sentence ends when one
    falls within ±30 % of the target; empty and whitespace-only chunks are skipped; at most
    ``max_chunks`` chunks. A text shorter than the target gives one chunk."""
    target = max(1, target_words)
    low = max(1, math.ceil(target * 0.7))
    high = max(low, math.floor(target * 1.3))
    words = list(re.finditer(r"\S+", text))
    chunks: list[Chunk] = []
    start = 0
    while start < len(words) and len(chunks) < max_chunks:
        remaining = len(words) - start
        if remaining <= target:
            size = remaining
        else:
            size = target
            best: int | None = None
            for n in range(low, min(high, remaining) + 1):
                if _SENTENCE_END.search(words[start + n - 1].group(0)) and (
                    best is None or abs(n - target) < abs(best - target)
                ):
                    best = n
            if best is not None:
                size = best
        piece = text[words[start].start() : words[start + size - 1].end()].strip()
        if piece:
            chunks.append(Chunk(source_key, len(chunks), piece))
        start += size
    return chunks


def bm25_rank(
    query_terms: Sequence[str], chunks: Sequence[Chunk], *, k1: float = 1.2, b: float = 0.75
) -> list[RankedChunk]:
    """Okapi BM25 over ``chunks`` (IDF with the +1 smoothing of Lucene), sorted by
    descending score then (source_key, position). ``simhash`` is ``simhash64`` of the
    normalized chunk text (``tower.core.domain.text``). Zero-score chunks are dropped."""
    terms: list[str] = []
    for raw in query_terms:
        for token in tokenize(raw):
            if token not in terms:
                terms.append(token)
    if not terms or not chunks:
        return []
    docs = [Counter(tokenize(c.text)) for c in chunks]
    lengths = [sum(d.values()) for d in docs]
    avgdl = (sum(lengths) / len(lengths)) or 1.0
    n_docs = len(chunks)
    idf = {}
    for term in terms:
        df = sum(1 for d in docs if term in d)
        idf[term] = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
    ranked: list[RankedChunk] = []
    for chunk, doc, length in zip(chunks, docs, lengths, strict=True):
        score = 0.0
        for term in terms:
            tf = doc.get(term, 0)
            if tf:
                score += idf[term] * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / avgdl))
        if score > 0:
            ranked.append(RankedChunk(chunk, score, simhash64(normalize(chunk.text))))
    ranked.sort(key=lambda r: (-r.score, r.chunk.source_key, r.chunk.position))
    return ranked


def dedupe_chunks(ranked: Sequence[RankedChunk], *, max_bits: int = 3) -> list[RankedChunk]:
    """Keep the best-scored chunk of each near-duplicate set (SimHash distance ≤
    ``max_bits``), preserving the order."""
    by_score = sorted(range(len(ranked)), key=lambda i: -ranked[i].score)
    kept: list[int] = []
    for i in by_score:
        if all(hamming(ranked[i].simhash, ranked[j].simhash) > max_bits for j in kept):
            kept.append(i)
    return [ranked[i] for i in sorted(kept)]


def select_passages(
    ranked: Sequence[RankedChunk], *, limit: int, per_source: int = 4
) -> list[RankedChunk]:
    """Top ``limit`` after ``dedupe_chunks``, at most ``per_source`` per source."""
    if limit <= 0:
        return []
    selected: list[RankedChunk] = []
    per: dict[str, int] = {}
    for item in dedupe_chunks(ranked):
        if per.get(item.chunk.source_key, 0) >= per_source:
            continue
        per[item.chunk.source_key] = per.get(item.chunk.source_key, 0) + 1
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


_WORD = re.compile(r"[^\W_]+")
_SENTENCE_END = re.compile(r"[.!?\u2026][\"'\u00bb\u201d)\]]*$")


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
