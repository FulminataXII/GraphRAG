"""`VectorRetriever` — hybrid dense+sparse chunk retrieval, read-through cached. See
BLUEPRINT §6.3.

JUDGMENT CALL — `hybrid_search`'s `rrf_k`/`weights` params: BLUEPRINT §5.2/§3.5 documents
`VectorStore.hybrid_search(weights=...)` as keyed by NAMED VECTOR ("dense", "bm25" — the same
strings used as `using=` on each `Prefetch`), and "a key that names no configured vector is a
ValidationError, not a silent no-op." `retrieval.fusion.weights` in config is keyed `{vector:
1.0, graph: 1.0}` — the CROSS-store names `fusion.py`'s own `reciprocal_rank_fusion` consumes,
not "dense"/"bm25". Passing `fusion.weights` straight through as `hybrid_search`'s `weights`
(as ARCHITECTURE §2.5's sample code, `weights=cfg.weights`, appears to suggest) would therefore
raise a ValidationError against the real adapter, not silently misweight. This module passes
`weights=None` (BLUEPRINT's own documented "None means equal weighting" default) for the
intra-Qdrant dense/sparse leg, and reuses `retrieval.fusion.rrf_k` — a single global RRF
constant, consistent with ARCHITECTURE's own sample using one `cfg.rrf_k` for both the
intra-store and cross-store fusion stages — since no dedicated dense/sparse `rrf_k` config leaf
exists either. Reported alongside the rest of this BO.

Constructor: scalars only (BLUEPRINT §0 — "a section model as a constructor parameter is fine"
for `services/`, but `get_settings()`/whole-`Settings` is not; see §5.2). Every value `retrieve()`
needs is threaded through individually rather than as `retrieval.vector`/`cache.retrieval`/
`config_hash` read off a `Settings` object.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from graphrag.core.models import ScoredChunk

if TYPE_CHECKING:
    from graphrag.core.ports import Cache, DocumentLedger, Embedder, VectorStore


def _cache_key(prefix: str, query: str, top_k: int, config_hash: str, corpus_version: int) -> str:
    payload = f"{query}\x1f{top_k}\x1f{config_hash}\x1f{corpus_version}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}{digest}"


class VectorRetriever:
    """Implements `retrieve()` (BLUEPRINT §6.3).

    Contract of retrieve(query, top_k) -> list[ScoredChunk]:
        - Embeds the query with is_query=True.
        - One hybrid_search call; results carry origin='vector' and dense 1-based ranks.
        - Read-through cache keyed on (query, params, config_hash, corpus_version).
        - Raises RetrievalBackendUnavailable on store failure — the caller decides to degrade.

    "results carry origin='vector'": `VectorStore.hybrid_search` (the Qdrant adapter) already
    fuses dense+sparse SERVER-SIDE and labels its own output `origin='fused'` (intra-store RRF,
    BLUEPRINT §2.5's "Server-side" row). From the CROSS-store perspective this whole call is
    just "the vector leg", so `retrieve()` relabels every result to `origin='vector'` and
    recomputes 1-based ranks from the returned order, rather than trusting the adapter's own
    (intra-store) rank/origin values.
    """

    def __init__(
        self,
        *,
        vector_store: VectorStore,
        embedder: Embedder,
        cache: Cache,
        ledger: DocumentLedger,
        prefetch_limit: int,
        rrf_k: int,
        cache_prefix: str,
        cache_ttl_s: int,
        cache_enabled: bool,
        config_hash: str,
    ) -> None:
        self._vector_store = vector_store
        self._embedder = embedder
        self._cache = cache
        self._ledger = ledger
        self._prefetch_limit = prefetch_limit
        self._rrf_k = rrf_k
        self._cache_prefix = cache_prefix
        self._cache_ttl_s = cache_ttl_s
        self._cache_enabled = cache_enabled
        self._config_hash = config_hash

    async def retrieve(self, query: str, top_k: int) -> list[ScoredChunk]:
        corpus_version = await self._ledger.current_corpus_version()
        cache_key = _cache_key(self._cache_prefix, query, top_k, self._config_hash, corpus_version)

        if self._cache_enabled:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return [ScoredChunk.model_validate(row) for row in json.loads(cached)]

        [dense_vector] = await self._embedder.embed_dense([query], is_query=True)
        [sparse_vector] = await self._embedder.embed_sparse([query])
        raw = await self._vector_store.hybrid_search(
            dense=dense_vector,
            sparse=sparse_vector,
            top_k=top_k,
            prefetch_limit=self._prefetch_limit,
            rrf_k=self._rrf_k,
            weights=None,
            filters=None,
        )
        results = [
            scored.model_copy(update={"origin": "vector", "rank": rank})
            for rank, scored in enumerate(raw, start=1)
        ]

        if self._cache_enabled:
            payload = json.dumps([r.model_dump(mode="json") for r in results]).encode("utf-8")
            await self._cache.set(cache_key, payload, ttl_s=self._cache_ttl_s)

        return results


__all__ = ["VectorRetriever"]
