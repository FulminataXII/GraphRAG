"""`cluster`, `choose_canonical`. See BLUEPRINT §6.2.

Union-find via `networkx.utils.UnionFind` — the ARCHITECTURE §4.3 tech choice
("RapidFuzz + NetworkX/union-find").
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from networkx.utils import UnionFind

from graphrag.core.errors import ValidationError

if TYPE_CHECKING:
    from graphrag.core.models import Mention


def cluster(pairs: Sequence[tuple[UUID, UUID]], *, max_cluster_size: int) -> list[set[UUID]]:
    """Union-find over merge edges; returns transitively closed clusters.

    Contract:
        - A~B and B~C implies one cluster {A,B,C}.
        - Raises ValidationError if any cluster exceeds max_cluster_size (tripwire for a
          mis-tuned threshold silently merging everything).

    Only nodes that appear in at least one pair are returned (each in exactly one cluster of
    size >= 2). A node with zero merge edges is not a "cluster" in this sense — the caller
    treats it as its own singleton entity.
    """
    uf: UnionFind[UUID] = UnionFind()
    nodes: set[UUID] = set()
    for a, b in pairs:
        uf.union(a, b)
        nodes.add(a)
        nodes.add(b)

    groups: dict[UUID, set[UUID]] = {}
    for node in nodes:
        root = uf[node]
        groups.setdefault(root, set()).add(node)

    clusters = list(groups.values())
    for members in clusters:
        if len(members) > max_cluster_size:
            raise ValidationError(
                f"resolution cluster of size {len(members)} exceeds "
                f"resolution.max_cluster_size={max_cluster_size}; this almost always means a "
                "scorer threshold is mis-tuned and merging everything together",
                details={"cluster_size": len(members), "max_cluster_size": max_cluster_size},
            )
    return clusters


def choose_canonical(members: Sequence[Mention]) -> str:
    """Most frequent surface form; ties broken by longest, then lexicographically. Deterministic."""
    counts = Counter(m.surface for m in members)
    top = max(counts.values())
    tied = [surface for surface, count in counts.items() if count == top]
    return sorted(tied, key=lambda s: (-len(s), s))[0]


__all__ = ["choose_canonical", "cluster"]
