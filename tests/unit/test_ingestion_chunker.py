"""`chunk_document` unit tests. See BLUEPRINT §6.1 / BUILD_ORDER BO-05."""

from __future__ import annotations

from itertools import pairwise

from graphrag.services.ingestion.chunker import chunk_document
from graphrag.services.ingestion.parser import ParsedDocument

_SENTENCE = (
    "The quick brown fox jumps over the lazy dog near the riverbank each morning before dawn."
)


def _prose(paragraphs: int, sentences_per_paragraph: int) -> str:
    para = " ".join(f"{_SENTENCE} Sentence number {i}." for i in range(sentences_per_paragraph))
    return "\n\n".join(f"Paragraph {p}. {para}" for p in range(paragraphs))


def _doc(text: str, page_offsets: list[tuple[int, int]] | None = None) -> ParsedDocument:
    return ParsedDocument(
        text=text, title=None, page_offsets=page_offsets or [], mime_type="text/plain"
    )


def test_chunker_offsets_index_exactly() -> None:
    text = _prose(paragraphs=12, sentences_per_paragraph=5)
    doc = _doc(text)
    chunks = chunk_document(doc, chunk_size=400, chunk_overlap=80, min_chunk_chars=50)

    assert len(chunks) > 1
    for chunk in chunks:
        assert doc.text[chunk.char_start : chunk.char_end] == chunk.text


def test_chunker_respects_size_and_overlap() -> None:
    text = _prose(paragraphs=15, sentences_per_paragraph=6)
    doc = _doc(text)
    chunk_size, chunk_overlap = 500, 100
    chunks = chunk_document(
        doc, chunk_size=chunk_size, chunk_overlap=chunk_overlap, min_chunk_chars=50
    )

    assert len(chunks) > 2
    for chunk in chunks:
        assert len(chunk.text) <= chunk_size

    for a, b in pairwise(chunks):
        overlap = a.char_end - b.char_start
        assert overlap >= chunk_overlap, (
            f"chunk {a.ord}->{b.ord} overlap {overlap} < required {chunk_overlap}"
        )


def test_chunker_merges_short_tail() -> None:
    """A final sentence far shorter than min_chunk_chars must be folded into the previous
    chunk, not left standing alone (unless it's the only chunk)."""
    body = _prose(paragraphs=6, sentences_per_paragraph=6)
    tiny_tail = "\n\nOk."
    text = body + tiny_tail
    doc = _doc(text)
    chunks = chunk_document(doc, chunk_size=400, chunk_overlap=80, min_chunk_chars=50)

    assert chunks[-1].char_end == len(text)
    assert "Ok." in chunks[-1].text
    assert len(chunks[-1].text) >= 50 or len(chunks) == 1


def test_chunker_min_chunk_chars_only_chunk_is_kept() -> None:
    """A document entirely shorter than chunk_size AND min_chunk_chars still yields one
    chunk — it is never dropped for being short."""
    doc = _doc("Hi.")
    chunks = chunk_document(doc, chunk_size=900, chunk_overlap=150, min_chunk_chars=80)
    assert len(chunks) == 1
    assert chunks[0].text == "Hi."


def test_chunker_empty_document_returns_no_chunks() -> None:
    doc = _doc("")
    assert chunk_document(doc, chunk_size=900, chunk_overlap=150, min_chunk_chars=80) == []


def test_chunker_page_assigned_from_page_offsets() -> None:
    page_one = "First page sentence one. First page sentence two. " * 5
    page_two = "Second page sentence one. Second page sentence two. " * 5
    text = page_one + "\n\n" + page_two
    offsets = [(0, len(page_one)), (len(page_one) + 2, len(text))]
    doc = _doc(text, page_offsets=offsets)

    chunks = chunk_document(doc, chunk_size=200, chunk_overlap=40, min_chunk_chars=30)

    pages_seen = {c.page for c in chunks}
    assert pages_seen == {1, 2}
    for chunk in chunks:
        if chunk.char_start < len(page_one):
            assert chunk.page == 1
        else:
            assert chunk.page == 2
