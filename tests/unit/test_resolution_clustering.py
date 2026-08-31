"""`cluster`, `choose_canonical` unit tests. See BLUEPRINT §6.2 / BUILD_ORDER BO-07."""

from __future__ import annotations

from uuid import uuid4

import pytest

from graphrag.core.errors import ValidationError
from graphrag.core.models import EntityType, Mention
from graphrag.services.resolution.clustering import choose_canonical, cluster


def _mention(surface: str) -> Mention:
    return Mention(
        surface=surface,
        type=EntityType.ORG,
        chunk_id=uuid4(),
        char_start=0,
        char_end=len(surface),
        confidence=0.9,
    )


def test_union_find_transitivity() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    clusters = cluster([(a, b), (b, c)], max_cluster_size=50)
    assert clusters == [{a, b, c}]


def test_disjoint_pairs_stay_separate() -> None:
    a, b, c, d = uuid4(), uuid4(), uuid4(), uuid4()
    clusters = cluster([(a, b), (c, d)], max_cluster_size=50)
    assert {frozenset(members) for members in clusters} == {
        frozenset({a, b}),
        frozenset({c, d}),
    }


def test_empty_pairs_yields_no_clusters() -> None:
    assert cluster([], max_cluster_size=50) == []


def test_max_cluster_size_tripwire() -> None:
    nodes = [uuid4() for _ in range(10)]
    pairs = [(nodes[i], nodes[i + 1]) for i in range(len(nodes) - 1)]  # one chain of 10
    with pytest.raises(ValidationError):
        cluster(pairs, max_cluster_size=5)


def test_choose_canonical_most_frequent() -> None:
    members = [_mention("Acme"), _mention("Acme"), _mention("ACME Corporation")]
    assert choose_canonical(members) == "Acme"


def test_choose_canonical_ties_broken_by_length_then_lex() -> None:
    members = [_mention("Acme"), _mention("ACME")]
    # tie on frequency (1 each) and length (4 each) -> lexicographically first
    assert choose_canonical(members) == "ACME"


def test_choose_canonical_deterministic() -> None:
    members = [_mention("Acme Corp."), _mention("ACME Corporation"), _mention("Acme")]
    first = choose_canonical(members)
    second = choose_canonical(list(reversed(members)))
    assert first == second == "ACME Corporation"  # longest surface, only one occurrence each
