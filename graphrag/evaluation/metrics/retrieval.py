"""Retrieval metrics — pure functions, no LLM, exactly testable. See BLUEPRINT §8.

All metrics use binary relevance: a retrieved chunk is relevant iff it appears in the gold
set. Scores are in [0, 1].
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from uuid import UUID


def recall_at_k(retrieved: Sequence[UUID], gold: Sequence[UUID], k: int) -> float:
    """Fraction of gold chunks found in the top *k* retrieved.

    Returns 0.0 when *gold* is empty (unanswerable items).
    """
    if not gold:
        return 0.0
    top_k = set(retrieved[:k])
    gold_set = set(gold)
    return len(top_k & gold_set) / len(gold_set)


def mrr(retrieved: Sequence[UUID], gold: Sequence[UUID]) -> float:
    """Mean Reciprocal Rank — 1 / rank of the first gold chunk in *retrieved*.

    Returns 0.0 when no gold chunk appears in the retrieved list, or when *gold* is empty.
    """
    if not gold:
        return 0.0
    gold_set = set(gold)
    for rank, chunk_id in enumerate(retrieved, start=1):
        if chunk_id in gold_set:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: Sequence[UUID], gold: Sequence[UUID], k: int) -> float:
    """Normalised Discounted Cumulative Gain at *k* with binary relevance.

    Contract:
        - Monotonic: improving an item's rank never lowers the score.
        - Returns 0.0 when *gold* is empty.
    """
    if not gold:
        return 0.0
    gold_set = set(gold)
    top_k = retrieved[:k]

    # DCG: sum of 1 / log2(rank+1) for relevant items in the top-k
    dcg = 0.0
    for rank, chunk_id in enumerate(top_k, start=1):
        if chunk_id in gold_set:
            dcg += 1.0 / math.log2(rank + 1)

    # Ideal DCG: the best possible arrangement — all gold items first
    ideal_relevant = min(len(gold_set), k)
    idcg = sum(1.0 / math.log2(r + 1) for r in range(1, ideal_relevant + 1))

    if idcg == 0.0:
        return 0.0
    return dcg / idcg
