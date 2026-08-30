"""LLM output schema tests. See BLUEPRINT §6.4."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from graphrag.services.orchestration.schemas import AnswerOut, CitationOut, RelevanceGrade


def test_out_schemas_use_str_ids_not_uuid() -> None:
    """Identifiers are `str`, never UUID — a hallucinated id must be a deterministic membership
    check (`verify_citations`), not a repair-burning parse failure."""
    assert CitationOut.model_fields["chunk_id"].annotation is str
    assert RelevanceGrade.model_fields["chunk_id"].annotation is str


def test_answer_out_requires_at_least_one_citation() -> None:
    with pytest.raises(ValidationError):
        AnswerOut(text="no support in context", citations=[], confidence=0.5)

    # One citation is accepted.
    answer = AnswerOut(
        text="Acme acquired Widgets Inc.",
        citations=[CitationOut(chunk_id="chunk-1", quote="Acme acquired Widgets Inc.")],
        confidence=0.9,
    )
    assert len(answer.citations) == 1
