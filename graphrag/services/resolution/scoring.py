"""`score_pair`, `decide`. See BLUEPRINT §6.2.

RapidFuzz is the ARCHITECTURE §4.3 tech choice ("RapidFuzz + NetworkX/union-find"). Both
`JaroWinkler.normalized_similarity` and `fuzz.token_set_ratio` are symmetric in their arguments
(Jaro-Winkler's prefix bonus depends only on the two strings' common prefix length, and
token_set_ratio operates on unordered token sets) — combined with a caller-supplied `cosine`
that is symmetric by construction, `score_pair` is symmetric end to end.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

if TYPE_CHECKING:
    from graphrag.core.models import EntityType, ScorerWeights


def score_pair(
    a_name: str,
    b_name: str,
    a_type: EntityType,
    b_type: EntityType,
    cosine: float,
    weights: ScorerWeights,
    require_type_match: bool,
) -> float:
    """Contract:
    - Returns 0.0 immediately when require_type_match and types differ. No exceptions.
    - Otherwise w1*jaro_winkler + w2*token_set_ratio + w3*cosine, all in [0,1].
    - Pure, deterministic, symmetric: score(a,b) == score(b,a).
    """
    if require_type_match and a_type != b_type:
        return 0.0

    jaro_winkler = JaroWinkler.normalized_similarity(a_name, b_name)
    token_set_ratio = fuzz.token_set_ratio(a_name, b_name) / 100.0

    score = (
        weights.jaro_winkler * jaro_winkler
        + weights.token_set_ratio * token_set_ratio
        + weights.embedding_cosine * cosine
    )
    return max(0.0, min(1.0, score))


def decide(score: float, merge_t: float, reject_t: float) -> Literal["merge", "gray", "reject"]:
    """score >= merge_t -> merge; score <= reject_t -> reject; otherwise gray."""
    if score >= merge_t:
        return "merge"
    if score <= reject_t:
        return "reject"
    return "gray"


__all__ = ["decide", "score_pair"]
