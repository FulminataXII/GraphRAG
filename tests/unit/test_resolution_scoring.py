"""`score_pair`, `decide` unit tests. See BLUEPRINT §6.2 / BUILD_ORDER BO-07."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from graphrag.core.models import EntityType, ScorerWeights
from graphrag.services.resolution.scoring import decide, score_pair

WEIGHTS = ScorerWeights(jaro_winkler=0.30, token_set_ratio=0.30, embedding_cosine=0.40)


def test_type_gate_blocks_merge() -> None:
    """`Apple(ORG)` vs `Apple(PRODUCT)` at cosine 0.99 -> 0.0, not "high but filtered later"."""
    score = score_pair("apple", "apple", EntityType.ORG, EntityType.PRODUCT, 0.99, WEIGHTS, True)
    assert score == 0.0


def test_type_gate_off_still_scores() -> None:
    score = score_pair("apple", "apple", EntityType.ORG, EntityType.PRODUCT, 0.99, WEIGHTS, False)
    assert score > 0.0


def test_identical_names_score_near_one() -> None:
    score = score_pair("acme", "acme", EntityType.ORG, EntityType.ORG, 1.0, WEIGHTS, True)
    assert score > 0.99


def test_unrelated_names_score_low() -> None:
    score = score_pair(
        "acme corporation", "widgets holdings", EntityType.ORG, EntityType.ORG, 0.1, WEIGHTS, True
    )
    assert score < 0.4


@given(
    st.text(min_size=1, max_size=40),
    st.text(min_size=1, max_size=40),
    st.sampled_from(list(EntityType)),
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
def test_score_symmetric(a: str, b: str, entity_type: EntityType, cosine: float) -> None:
    forward = score_pair(a, b, entity_type, entity_type, cosine, WEIGHTS, True)
    backward = score_pair(b, a, entity_type, entity_type, cosine, WEIGHTS, True)
    assert forward == backward


def test_decide_band_boundaries() -> None:
    assert decide(0.90, merge_t=0.90, reject_t=0.65) == "merge"
    assert decide(0.65, merge_t=0.90, reject_t=0.65) == "reject"
    assert decide(0.899999, merge_t=0.90, reject_t=0.65) == "gray"
    assert decide(0.650001, merge_t=0.90, reject_t=0.65) == "gray"
    assert decide(1.0, merge_t=0.90, reject_t=0.65) == "merge"
    assert decide(0.0, merge_t=0.90, reject_t=0.65) == "reject"
