from __future__ import annotations

from typing import get_args
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from graphrag.core.models import (
    BudgetLimits,
    DocumentStatus,
    JobStatus,
    Relation,
    SparseVector,
    Spend,
)

_spend_strategy = st.builds(
    Spend,
    llm_calls=st.integers(min_value=0, max_value=10_000),
    tokens=st.integers(min_value=0, max_value=10_000_000),
    wall_ms=st.integers(min_value=0, max_value=10_000_000),
)


@given(a=_spend_strategy, b=_spend_strategy)
def test_spend_merge_commutative(a: Spend, b: Spend) -> None:
    assert Spend.merge(a, b) == Spend.merge(b, a)


@given(a=_spend_strategy, b=_spend_strategy, c=_spend_strategy)
def test_spend_merge_associative(a: Spend, b: Spend, c: Spend) -> None:
    assert Spend.merge(Spend.merge(a, b), c) == Spend.merge(a, Spend.merge(b, c))


def test_spend_wall_ms_uses_max_not_sum() -> None:
    a = Spend(llm_calls=1, tokens=100, wall_ms=300)
    b = Spend(llm_calls=2, tokens=200, wall_ms=500)
    merged = Spend.merge(a, b)
    assert merged.wall_ms == 500
    assert merged.llm_calls == 3
    assert merged.tokens == 300


def test_spend_exceeds_names_first_breach() -> None:
    limits = BudgetLimits(max_llm_calls=5, max_wall_ms=1000, max_prompt_tokens=2000)
    spend = Spend(llm_calls=6, tokens=2500, wall_ms=1500)
    assert spend.exceeds(limits) == "max_llm_calls"
    assert Spend(llm_calls=1, tokens=100, wall_ms=1500).exceeds(limits) == "max_wall_ms"
    assert Spend(llm_calls=1, tokens=2500, wall_ms=1).exceeds(limits) == "max_prompt_tokens"
    assert Spend(llm_calls=1, tokens=1, wall_ms=1).exceeds(limits) is None


def test_relation_requires_provenance() -> None:
    with pytest.raises(ValidationError):
        Relation(
            src_id=uuid4(),
            dst_id=uuid4(),
            type="WORKS_AT",
            confidence=0.9,
            chunk_id=None,  # type: ignore[arg-type]
            doc_id="doc-1",
            evidence_span="he works at Acme",
        )


def test_empty_sparse_vector_is_valid() -> None:
    vec = SparseVector(indices=[], values=[])
    assert vec.indices == []
    assert vec.values == []


def test_sparse_vector_arrays_aligned() -> None:
    with pytest.raises(ValidationError):
        SparseVector(indices=[1, 2], values=[0.5])


def test_job_status_distinct_from_document_status() -> None:
    state_annotation = JobStatus.model_fields["state"].annotation
    job_states = set(get_args(state_annotation))
    doc_statuses = {member.value for member in DocumentStatus}
    assert job_states.isdisjoint(doc_statuses)
