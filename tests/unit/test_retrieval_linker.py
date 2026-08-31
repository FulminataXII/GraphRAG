"""`EntityLinker` unit tests. See BLUEPRINT §6.3.

`tests.fakes.FakeVectorStore.search_entities` returns a fixed score of 1.0 for every match
(correct for what it exists to test elsewhere — resolution blocking doesn't care about linker
thresholds), which makes `min_score` filtering untestable through it. `_ScoredEntityStore`
below is a small, purpose-built local double (not a mock — real behaviour, just a minimal one,
same convention as `tests/unit/test_resolution_blocking.py`'s `_CosineVectorStore`) that returns
caller-configured scores directly.
"""

from __future__ import annotations

from graphrag.core.models import Entity
from graphrag.services.retrieval.linker import EntityLinker
from tests.factories import make_entity
from tests.fakes import FakeEmbedder


class _ScoredEntityStore:
    """Implements only what `EntityLinker` actually calls (`search_entities`); scores are
    whatever the test configures, not derived from the query vector at all."""

    def __init__(self, scored: list[tuple[Entity, float]]) -> None:
        self._scored = scored

    async def search_entities(
        self, vector: list[float], *, top_k: int, entity_type=None
    ) -> list[tuple[Entity, float]]:
        return self._scored[:top_k]


async def test_linker_returns_empty_on_miss() -> None:
    """Nothing indexed at all -- a miss, not an error."""
    linker = EntityLinker(embedder=FakeEmbedder(), vector_store=_ScoredEntityStore([]))
    result = await linker.link("some query", top_k=5, min_score=0.5)
    assert result == []


async def test_linker_respects_min_score() -> None:
    high = make_entity(name="High Score Entity")
    low = make_entity(name="Low Score Entity")
    store = _ScoredEntityStore([(high, 0.95), (low, 0.10)])
    linker = EntityLinker(embedder=FakeEmbedder(), vector_store=store)

    result = await linker.link("some query", top_k=5, min_score=0.5)

    assert result == [high]
