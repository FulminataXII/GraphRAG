"""`Blocker`. See BLUEPRINT §6.2 / ARCHITECTURE §2.4.

SPEC GAP (reported alongside the rest of the BO-07 report): BLUEPRINT's sketch signature is
`candidates(mention, ...) -> list[Entity]` — the non-`mention` parameters are elided, and
`core.ports` exposes no "find entity by exact normalized name" query (only `VectorStore.
search_entities`, which is kNN-only, and only sees entities already persisted). This
implementation resolves that gap with THREE unioned legs rather than the two ARCHITECTURE §2.4
names, because a strict two-leg reading would only ever merge names within one `resolve()` batch
when they are byte-identical after normalization:

    1. Exact-name match against entities `index()`ed so far in this batch (cheap, O(1)).
    2. In-batch vector similarity against the same locally `index()`ed entities — this is what
       lets two DIFFERENT-looking mentions of the same real entity within one document (e.g. a
       full name and a bare surname mentioned later in the same article) merge in a single
       `resolve()` call, since neither has been persisted to `VectorStore` yet for the remote
       leg below to find.
    3. Remote vector kNN over the persisted `entities` Qdrant collection — this is what makes
       cross-document (and cross-run) resolution and idempotency work: an identical normalized
       name embeds to a (near-)identical vector, so a prior run's entity surfaces here with very
       high similarity.

Leg 2 is a linear scan over entities indexed so far in the CURRENT batch, so it is bounded by
document size (how many distinct entity groups one extraction batch produces), not corpus size —
it does not reintroduce the cross-corpus O(n^2) blocking exists to avoid, but a pathologically
large single-document batch would degrade. Noted as a scaling limit, not fixed here to stay in
scope.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from graphrag.config.schema import ResolutionSection
    from graphrag.core.models import Entity, EntityType
    from graphrag.core.ports import VectorStore


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class Blocker:
    """Candidate generation. Avoids O(n^2).

    Contract of candidates(name_normalized, entity_type, vector) -> list[Entity]:
        - Union of (a) exact normalized-name match, (b) in-batch vector similarity, and (c)
          remote vector kNN over the `entities` Qdrant collection — see module docstring for why
          three legs. (b) and (c) are each capped at resolution.block_k, filtered by type when
          require_type_match.
        - Total pairwise comparisons across n mentions must be <= n * block_k * 1.2.
    """

    def __init__(self, *, vector_store: VectorStore, resolution: ResolutionSection) -> None:
        self._vector_store = vector_store
        self._resolution = resolution
        self._by_normalized_name: dict[tuple[str, EntityType], list[Entity]] = {}
        self._local: dict[UUID, tuple[Entity, list[float]]] = {}

    def index(self, entities: Sequence[Entity], vectors: Sequence[list[float]]) -> None:
        """Publish entities minted earlier in the same `resolve()` batch so later mentions in
        that batch can find them via the exact-name and in-batch-vector legs."""
        for entity, vector in zip(entities, vectors, strict=True):
            key = (entity.name_normalized, entity.type)
            self._by_normalized_name.setdefault(key, []).append(entity)
            self._local[entity.canonical_id] = (entity, vector)

    async def candidates(
        self, name_normalized: str, entity_type: EntityType, vector: list[float]
    ) -> list[Entity]:
        block_k = self._resolution.block_k
        require_type = self._resolution.require_type_match

        exact = self._by_normalized_name.get((name_normalized, entity_type), [])

        local_scored = [
            (entity, cosine_similarity(vector, cand_vector))
            for entity, cand_vector in self._local.values()
            if not require_type or entity.type == entity_type
        ]
        local_scored.sort(key=lambda pair: pair[1], reverse=True)
        local_hits = [entity for entity, _score in local_scored[:block_k]]

        remote_hits = await self._vector_store.search_entities(
            vector, top_k=block_k, entity_type=entity_type if require_type else None
        )

        merged: dict[UUID, Entity] = {entity.canonical_id: entity for entity in exact}
        for entity in local_hits:
            merged.setdefault(entity.canonical_id, entity)
        for entity, _score in remote_hits:
            merged.setdefault(entity.canonical_id, entity)
        return list(merged.values())


__all__ = ["Blocker", "cosine_similarity"]
