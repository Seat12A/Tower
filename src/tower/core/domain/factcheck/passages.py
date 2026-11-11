"""Passages (pipeline.md E7): chunking, lexical ranking (BM25), selection and
near-duplicate removal by SimHash, in pure Python (no numpy)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

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
    raise NotImplementedError


def chunk_text(
    source_key: str, text: str, *, target_words: int = TARGET_WORDS, max_chunks: int = 200
) -> list[Chunk]:
    """Split into chunks of about ``target_words`` words, cutting at sentence ends when one
    falls within ±30 % of the target; empty and whitespace-only chunks are skipped; at most
    ``max_chunks`` chunks. A text shorter than the target gives one chunk."""
    raise NotImplementedError


def bm25_rank(
    query_terms: Sequence[str], chunks: Sequence[Chunk], *, k1: float = 1.2, b: float = 0.75
) -> list[RankedChunk]:
    """Okapi BM25 over ``chunks`` (IDF with the +1 smoothing of Lucene), sorted by
    descending score then (source_key, position). ``simhash`` is ``simhash64`` of the
    normalized chunk text (``tower.core.domain.text``). Zero-score chunks are dropped."""
    raise NotImplementedError


def dedupe_chunks(ranked: Sequence[RankedChunk], *, max_bits: int = 3) -> list[RankedChunk]:
    """Keep the best-scored chunk of each near-duplicate set (SimHash distance ≤
    ``max_bits``), preserving the order."""
    raise NotImplementedError


def select_passages(
    ranked: Sequence[RankedChunk], *, limit: int, per_source: int = 4
) -> list[RankedChunk]:
    """Top ``limit`` after ``dedupe_chunks``, at most ``per_source`` per source."""
    raise NotImplementedError
