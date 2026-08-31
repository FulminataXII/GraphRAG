"""`reciprocal_rank_fusion` unit tests, including the BO-09 known-input gate. See BLUEPRINT §6.3.

Pure function, no I/O — every test here is a plain unit test.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Literal

import pytest

from graphrag.core.models import ScoredChunk
from graphrag.services.retrieval.fusion import reciprocal_rank_fusion
from tests.factories import make_chunk


def _sc(
    text: str, *, rank: int, origin: Literal["vector", "graph", "fused"] = "vector"
) -> ScoredChunk:
    return ScoredChunk(chunk=make_chunk(text), score=0.0, rank=rank, origin=origin)


def test_rrf_known_input() -> None:
    """Hand-computed on two 3-item lists, exact order (BUILD_ORDER gate).

    list1 (vector): A@1, B@2, C@3
    list2 (graph):   C@1, A@2, D@3
    k=60, weights=[1, 1]

    score(A) = 1/61 + 1/62
    score(B) = 1/62
    score(C) = 1/63 + 1/61
    score(D) = 1/63

    A (1/61+1/62) > C (1/63+1/61) since 1/62 > 1/63, so A ranks above C; C (1/63+1/61) > B
    (1/62) and > D (1/63) since C accumulates from both lists. Expected fused order: A, C, B, D.
    """
    a, b, c, d = "chunk A", "chunk B", "chunk C", "chunk D"
    list1 = [_sc(a, rank=1), _sc(b, rank=2), _sc(c, rank=3)]
    list2 = [
        _sc(c, rank=1, origin="graph"),
        _sc(a, rank=2, origin="graph"),
        _sc(d, rank=3, origin="graph"),
    ]

    fused = reciprocal_rank_fusion([list1, list2], k=60, weights=[1.0, 1.0], top_n=4)

    assert [f.chunk.text for f in fused] == [a, c, b, d]
    assert [f.rank for f in fused] == [1, 2, 3, 4]
    assert all(f.origin == "fused" for f in fused)

    k = 60
    expected_a = Fraction(1, k + 1) + Fraction(1, k + 2)
    expected_c = Fraction(1, k + 3) + Fraction(1, k + 1)
    expected_b = Fraction(1, k + 2)
    expected_d = Fraction(1, k + 3)
    for chunk, expected in zip(
        fused, [expected_a, expected_c, expected_b, expected_d], strict=True
    ):
        assert chunk.score == pytest.approx(float(expected))


def test_rrf_handles_empty_list() -> None:
    assert reciprocal_rank_fusion([], k=60, weights=[], top_n=10) == []
    assert reciprocal_rank_fusion([[], []], k=60, weights=[1.0, 1.0], top_n=10) == []

    only_one_populated = [[_sc("solo chunk", rank=1)], []]
    fused = reciprocal_rank_fusion(only_one_populated, k=60, weights=[1.0, 1.0], top_n=10)
    assert [f.chunk.text for f in fused] == ["solo chunk"]


def test_rrf_deterministic_tiebreak() -> None:
    """Equal score -> lower best rank wins; equal score AND equal best rank -> chunk_id
    ascending."""
    # Equal total score (both rank 1 in one list only), but X appears at a better rank overall
    # in a SECOND list, Y doesn't -- X must win.
    x, y = "chunk X", "chunk Y"
    list1 = [_sc(x, rank=1), _sc(y, rank=1)]  # identical rank -> identical contribution
    list2 = [_sc(x, rank=1, origin="graph")]  # X gets a second, better-ranked contribution
    fused = reciprocal_rank_fusion([list1, list2], k=60, weights=[1.0, 1.0], top_n=10)
    assert next(f.chunk.text for f in fused) == x

    # Exactly equal score AND equal best rank (same chunk appears at rank 1 in both lists for
    # two different chunk ids -- symmetric setup) -> tie-break falls to chunk_id ascending.
    p, q = make_chunk("chunk P"), make_chunk("chunk Q")
    lo, hi = sorted([p.chunk_id, q.chunk_id])
    lo_chunk = p if p.chunk_id == lo else q
    hi_chunk = q if lo_chunk is p else p
    list_a = [ScoredChunk(chunk=lo_chunk, score=0.0, rank=1, origin="vector")]
    list_b = [ScoredChunk(chunk=hi_chunk, score=0.0, rank=1, origin="vector")]
    fused_tie = reciprocal_rank_fusion([list_a, list_b], k=60, weights=[1.0, 1.0], top_n=10)
    assert [f.chunk.chunk_id for f in fused_tie] == [lo, hi]


def test_rrf_weights_shift_order() -> None:
    """Changing weights alone flips which chunk ranks first.

    X and Y each appear only once, at rank 1, in their own separate list -- with equal weights
    their scores are exactly tied (tie-break would go to whichever has the lower chunk_id, an
    accident of content-hashing this test doesn't want to depend on). Strongly asymmetric
    weights break that tie deterministically in either direction, which is the property under
    test: the SAME two candidate lists, reweighted, produce a different winner.
    """
    x, y = "weighted X", "weighted Y"
    list1 = [_sc(x, rank=1)]  # X only ever appears in list1
    list2 = [_sc(y, rank=1, origin="graph")]  # Y only ever appears in list2

    y_favored = reciprocal_rank_fusion([list1, list2], k=60, weights=[0.1, 10.0], top_n=2)
    assert y_favored[0].chunk.text == y

    x_favored = reciprocal_rank_fusion([list1, list2], k=60, weights=[10.0, 0.1], top_n=2)
    assert x_favored[0].chunk.text == x


def test_rrf_ranks_are_1_based() -> None:
    list1 = [_sc(f"rank chunk {i}", rank=i) for i in range(1, 5)]
    fused = reciprocal_rank_fusion([list1], k=60, weights=[1.0], top_n=4)
    assert [f.rank for f in fused] == [1, 2, 3, 4]
