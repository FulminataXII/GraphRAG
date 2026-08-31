"""`VectorRetriever` unit tests (fakes only, no real Qdrant/Redis). See BLUEPRINT §6.3.

Live-backend behavior (cache invalidation across a real Redis, hybrid ranking against a real
Qdrant) is covered by `tests/integration/test_retrieval_vector.py`.
"""

from __future__ import annotations

import pytest

from graphrag.config.settings import Settings
from graphrag.core.errors import RetrievalBackendUnavailable
from graphrag.services.retrieval.vector import VectorRetriever
from tests.fakes import FakeCache, FakeDocumentLedger, FakeEmbedder, FakeVectorStore


def _retriever(
    settings: Settings,
    *,
    vector_store: FakeVectorStore | None = None,
    embedder: FakeEmbedder | None = None,
    cache: FakeCache | None = None,
    ledger: FakeDocumentLedger | None = None,
) -> VectorRetriever:
    return VectorRetriever(
        vector_store=vector_store if vector_store is not None else FakeVectorStore(),
        embedder=embedder if embedder is not None else FakeEmbedder(),
        cache=cache if cache is not None else FakeCache(),
        ledger=ledger if ledger is not None else FakeDocumentLedger(),
        prefetch_limit=settings.retrieval.vector.prefetch_limit,
        rrf_k=settings.retrieval.fusion.rrf_k,
        cache_prefix=settings.cache.retrieval.prefix,
        cache_ttl_s=settings.cache.retrieval.ttl_s,
        cache_enabled=settings.cache.retrieval.enabled,
        config_hash=settings.config_hash,
    )


async def test_vector_retriever_uses_query_prefix(settings: Settings) -> None:
    """`retrieve()` must embed the query with `is_query=True` -- the embedder applies
    `embedding.dense.query_prefix` internally when this flag is set (BLUEPRINT §5.1); the caller
    only needs to pass the flag through."""
    embedder = FakeEmbedder()
    retriever = _retriever(settings, embedder=embedder)

    await retriever.retrieve("what is the part number", top_k=5)

    dense_calls = [c for c in embedder.dense_calls if c["texts"] == ["what is the part number"]]
    assert dense_calls, "embed_dense was never called with the query text"
    assert dense_calls[0]["is_query"] is True


async def test_vector_raises_backend_unavailable_on_failure(settings: Settings) -> None:
    """Store failure propagates -- the caller decides whether/how to degrade, `retrieve()`
    itself must not swallow it."""
    vector_store = FakeVectorStore()
    vector_store.fail = True
    retriever = _retriever(settings, vector_store=vector_store)

    with pytest.raises(RetrievalBackendUnavailable):
        await retriever.retrieve("a query", top_k=5)
