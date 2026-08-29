"""`DocumentParser` unit tests. See BLUEPRINT §6.1 / BUILD_ORDER BO-05."""

from __future__ import annotations

import pytest

from graphrag.core.errors import ValidationError
from graphrag.services.ingestion.parser import DOCX_MIME, PDF_MIME, DocumentParser


def test_parser_rejects_corrupt_input() -> None:
    """Corrupt bytes for a claimed mime type raise ValidationError, never a raw library
    exception (pymupdf/python-docx internals must never leak past this boundary)."""
    parser = DocumentParser()
    with pytest.raises(ValidationError):
        parser.parse(b"this is not a real pdf", PDF_MIME)
    with pytest.raises(ValidationError):
        parser.parse(b"this is not a real docx", DOCX_MIME)


def test_parser_rejects_unsupported_mime_type() -> None:
    parser = DocumentParser()
    with pytest.raises(ValidationError):
        parser.parse(b"hello", "application/octet-stream")


def test_parser_rejects_non_utf8_text() -> None:
    parser = DocumentParser()
    with pytest.raises(ValidationError):
        parser.parse(b"\xff\xfe not utf-8", "text/plain")


def test_parser_rejects_empty_text() -> None:
    parser = DocumentParser()
    with pytest.raises(ValidationError):
        parser.parse(b"   \n\n  ", "text/plain")


def test_parser_plain_text_roundtrips() -> None:
    parser = DocumentParser()
    parsed = parser.parse(b"Hello, world.\n\nSecond paragraph.", "text/plain")
    assert "Hello, world." in parsed.text
    assert parsed.page_offsets == []
    assert parsed.mime_type == "text/plain"


def test_parser_markdown_uses_same_path_as_plain_text() -> None:
    parser = DocumentParser()
    parsed = parser.parse(b"# Title\n\nBody text.", "text/markdown")
    assert "Body text." in parsed.text
    assert parsed.page_offsets == []
