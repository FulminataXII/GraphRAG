"""`ChunkSpec` / `chunk_document`. See BLUEPRINT §6.1.

Recursive character splitting: paragraph -> sentence -> word -> hard-character boundaries.
Pure and offset-exact — every chunk is a literal slice of `doc.text` (`doc.text[cs:ce] ==
chunk.text`), never a reconstruction, so overlap/merge bookkeeping only ever moves integer
offsets around.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict

from graphrag.services.ingestion.parser import ParsedDocument

_PARAGRAPH_RE: Final[re.Pattern[str]] = re.compile(r"\n\s*\n")
_SENTENCE_RE: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?])\s+")
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


class ChunkSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    ord: int
    char_start: int
    char_end: int
    page: int | None


def chunk_document(
    doc: ParsedDocument, *, chunk_size: int, chunk_overlap: int, min_chunk_chars: int
) -> list[ChunkSpec]:
    """Recursive character splitting on paragraph -> sentence -> word boundaries.

    Contract:
        - No chunk exceeds chunk_size characters.
        - Consecutive chunks overlap by >= chunk_overlap characters, EXCEPT where a hard
          boundary makes it impossible; the final chunk may be shorter.
        - Chunks shorter than min_chunk_chars are merged into the previous chunk, not dropped,
          unless they are the only chunk (or merging would push the previous chunk over
          chunk_size, in which case the short chunk is kept standing on its own — the
          chunk_size ceiling is the harder constraint of the two).
        - char_start/char_end index into doc.text exactly: doc.text[cs:ce] == chunk.text.
        - Pure and deterministic.
    """
    text = doc.text
    if not text:
        return []
    if len(text) <= chunk_size:
        spans: list[tuple[int, int]] = [(0, len(text))]
    else:
        units = _atomic_units(text, 0, len(text), chunk_size)
        spans = _pack(units, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        spans = _merge_short_tails(spans, min_chunk_chars=min_chunk_chars, chunk_size=chunk_size)

    return [
        ChunkSpec(
            text=text[start:end],
            ord=i,
            char_start=start,
            char_end=end,
            page=_page_for(start, doc.page_offsets),
        )
        for i, (start, end) in enumerate(spans)
    ]


def _cut(text: str, start: int, end: int, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    """Contiguous spans covering [start, end), split right after each separator match. A
    range with no match returns [(start, end)] unchanged — never an empty list."""
    cut_points = [m.end() for m in pattern.finditer(text, start, end)]
    spans: list[tuple[int, int]] = []
    prev = start
    for cut in cut_points:
        if cut > prev:
            spans.append((prev, cut))
        prev = cut
    if prev < end:
        spans.append((prev, end))
    return spans or [(start, end)]


def _atomic_units(text: str, start: int, end: int, chunk_size: int) -> list[tuple[int, int]]:
    """Split [start, end) down to SENTENCE granularity unconditionally (never stopping early
    just because a paragraph already fits chunk_size) — that granularity is what gives `_pack`
    room to satisfy the overlap requirement instead of landing exactly on paragraph edges.
    A sentence that still exceeds chunk_size is split further on word, then hard-character,
    boundaries. Units are contiguous: they cover the whole range with no gaps or overlap."""
    units: list[tuple[int, int]] = []
    for para_start, para_end in _cut(text, start, end, _PARAGRAPH_RE):
        for sent_start, sent_end in _cut(text, para_start, para_end, _SENTENCE_RE):
            if sent_end - sent_start <= chunk_size:
                units.append((sent_start, sent_end))
            else:
                units.extend(_split_oversized(text, sent_start, sent_end, chunk_size))
    return units


def _split_oversized(text: str, start: int, end: int, chunk_size: int) -> list[tuple[int, int]]:
    word_spans = _cut(text, start, end, _WORD_RE)
    if len(word_spans) == 1:
        return _hard_split(start, end, chunk_size)
    units: list[tuple[int, int]] = []
    for word_start, word_end in word_spans:
        if word_end - word_start <= chunk_size:
            units.append((word_start, word_end))
        else:
            units.extend(_hard_split(word_start, word_end, chunk_size))
    return units


def _hard_split(start: int, end: int, chunk_size: int) -> list[tuple[int, int]]:
    units = []
    pos = start
    while pos < end:
        nxt = min(pos + chunk_size, end)
        units.append((pos, nxt))
        pos = nxt
    return units


def _pack(
    units: Sequence[tuple[int, int]], *, chunk_size: int, chunk_overlap: int
) -> list[tuple[int, int]]:
    """Greedily fill each chunk with as many consecutive units as fit within chunk_size, then
    back the next chunk's start up into the current one until >= chunk_overlap characters are
    shared (or progress would stall, in which case one unit of overlap is accepted)."""
    chunks: list[tuple[int, int]] = []
    i = 0
    n = len(units)
    while i < n:
        chunk_start = units[i][0]
        j = i
        cur_end = units[i][1]
        while j + 1 < n and (units[j + 1][1] - chunk_start) <= chunk_size:
            j += 1
            cur_end = units[j][1]
        chunks.append((chunk_start, cur_end))
        if j == n - 1:
            break
        k = j
        while k > i and (cur_end - units[k][0]) < chunk_overlap:
            k -= 1
        i = k if k > i else i + 1
    return chunks


def _merge_short_tails(
    spans: list[tuple[int, int]], *, min_chunk_chars: int, chunk_size: int
) -> list[tuple[int, int]]:
    if len(spans) <= 1:
        return spans
    merged: list[tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        prev_start, _prev_end = merged[-1]
        if (end - start) < min_chunk_chars and (end - prev_start) <= chunk_size:
            merged[-1] = (prev_start, end)
        else:
            merged.append((start, end))
    return merged


def _page_for(char_start: int, page_offsets: Sequence[tuple[int, int]]) -> int | None:
    for i, (start, end) in enumerate(page_offsets, start=1):
        if start <= char_start < end:
            return i
    return None
