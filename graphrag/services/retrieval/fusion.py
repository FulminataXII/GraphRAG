"""`reciprocal_rank_fusion` — cross-store RRF. See BLUEPRINT §6.3.

Pure function: no I/O, no config object, no port dependency. The intra-store (dense vs sparse)
fusion happens server-side inside `VectorStore.hybrid_search` (BLUEPRINT §2.5's "Server-side"
row); this is stage 2, "Client-side, in Python" — merging the vector leg's `ScoredChunk` list
against the graph leg's, which live in two different databases that cannot fuse each other's
results.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from graphrag.core.models import ScoredChunk


def reciprocal_rank_fusion(
    lists: Sequence[Sequence[ScoredChunk]],
    *,
    k: int,
    weights: Sequence[float],
    top_n: int,
) -> list[ScoredChunk]:
    """Cross-store RRF.

    Contract:
        - score(d) = sum_i weights[i] / (k + rank_i(d)); ranks are 1-based (taken as given from
          each input list's own `ScoredChunk.rank` — this function does not renumber its inputs,
          only its output).
        - Documents absent from a list contribute nothing from that list.
        - An empty input list is legal and contributes nothing (sparse legitimately returns 0).
        - Deterministic tie-break: higher score, then lower best rank, then chunk_id ascending.
        - Returns top_n with origin='fused' and recomputed 1-based ranks.

    `len(weights) != len(lists)` is a caller error (a weight is meaningless without a
    corresponding list, and BLUEPRINT gives `weights` no default that could silently paper over
    a missing entry), so it raises `ValueError` rather than truncating or zero-filling.
    """
    if len(weights) != len(lists):
        raise ValueError(
            f"weights must have the same length as lists ({len(weights)} != {len(lists)})"
        )

    scores: dict[UUID, float] = {}
    best_rank: dict[UUID, int] = {}
    representative: dict[UUID, ScoredChunk] = {}

    for weight, scored_list in zip(weights, lists, strict=True):
        for scored in scored_list:
            chunk_id = scored.chunk.chunk_id
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (k + scored.rank)
            if chunk_id not in best_rank or scored.rank < best_rank[chunk_id]:
                best_rank[chunk_id] = scored.rank
                representative[chunk_id] = scored

    ordered = sorted(scores, key=lambda cid: (-scores[cid], best_rank[cid], cid))

    return [
        representative[chunk_id].model_copy(
            update={"score": scores[chunk_id], "rank": rank, "origin": "fused"}
        )
        for rank, chunk_id in enumerate(ordered[:top_n], start=1)
    ]


__all__ = ["reciprocal_rank_fusion"]
