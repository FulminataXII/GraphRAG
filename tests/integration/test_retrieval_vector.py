"""`VectorRetriever` against real Qdrant/Redis. See BLUEPRINT §6.3 / BUILD_ORDER BO-09.

Unit-level behavior (query-prefix wiring, backend-failure propagation) is covered by
`tests/unit/test_retrieval_vector.py`, which needs no live backend.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from qdrant_client import AsyncQdrantClient

from graphrag.adapters.qdrant_store import QdrantVectorStore
from graphrag.adapters.redis_cache import RedisCache
from graphrag.config.settings import Settings
from graphrag.core.models import SparseVector
from graphrag.services.retrieval.vector import VectorRetriever
from tests.factories import make_chunk, make_source_ref
from tests.fakes import FakeCache, FakeDocumentLedger, FakeVectorStore
from tests.integration import namespaces as ns
from tests.integration.conftest import drop_collections
from tests.unit._settings_helpers import set_required_secrets

pytestmark = pytest.mark.integration

_QDRANT_URL = "http://localhost:6333"
_DIM = 4  # small, hand-legible dense vectors -- same convention as test_qdrant_store.py


@pytest.fixture
def vector_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    set_required_secrets(monkeypatch)
    base = ns.namespaced(Settings(), local=uuid.uuid4().hex[:8])
    dense = base.embedding.dense.model_copy(update={"dimensions": _DIM})
    embedding = base.embedding.model_copy(update={"dense": dense})
    return base.model_copy(update={"embedding": embedding})


@pytest.fixture
async def qdrant_client() -> AsyncIterator[AsyncQdrantClient]:
    client = AsyncQdrantClient(url=_QDRANT_URL, prefer_grpc=False, timeout=10)
    yield client
    await client.close()


@pytest.fixture
async def store(
    qdrant_client: AsyncQdrantClient, vector_settings: Settings
) -> AsyncIterator[QdrantVectorStore]:
    yield QdrantVectorStore(qdrant_client, vector_settings)
    await drop_collections(qdrant_client, vector_settings.retrieval.vector.collection)


class _ScriptedEmbedder:
    """Returns pre-registered vectors for exact query-text matches. Not a mock (BLUEPRINT §9)
    — real, deterministic behaviour, just fully caller-controlled instead of computed, so this
    test can hand-craft the exact dense/sparse rank conflict it needs (same convention as
    `tests/unit/test_resolution_blocking.py`'s `_CosineVectorStore`)."""

    def __init__(
        self, *, dense: dict[str, list[float]], sparse: dict[str, SparseVector], dimensions: int
    ) -> None:
        self._dense = dense
        self._sparse = sparse
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_dense(
        self, texts: Sequence[str], *, is_query: bool = False
    ) -> list[list[float]]:
        return [self._dense[text] for text in texts]

    async def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]:
        return [self._sparse[text] for text in texts]


async def test_hybrid_beats_singles_on_exact_id(
    store: QdrantVectorStore, vector_settings: Settings
) -> None:
    """A chunk containing the exact literal part number ranks first under
    `VectorRetriever.retrieve()`'s hybrid dense+sparse search, even though a dense-only ranking
    would favor a different, semantically-closer chunk instead (BUILD_ORDER's own framing:
    "literal part number ranks higher under hybrid"). Mirrors
    `tests/integration/test_qdrant_store.py::test_sparse_beats_dense_on_rare_token`'s
    dense/sparse construction one layer up — through `VectorRetriever`'s
    embed -> hybrid_search -> relabel path, not by calling `hybrid_search` directly — so this
    proves VectorRetriever's own wiring doesn't break the property the adapter already
    guarantees.
    """
    await store.ensure_collections()

    query = "PN-77234-ZQ"
    dense_favorite = make_chunk(
        "dense favorite, no shared vocabulary", sources=[make_source_ref(doc_id="doc-dense")]
    )
    exact_match = make_chunk(
        "chunk mentioning part PN-77234-ZQ directly", sources=[make_source_ref(doc_id="doc-exact")]
    )
    query_dense = [1.0, 0.0, 0.0, 0.0]
    query_sparse = SparseVector(indices=[1], values=[1.0])  # stands in for the literal token

    await store.upsert_chunks(
        [dense_favorite, exact_match],
        [query_dense, [0.0, 0.0, 1.0, 0.0]],  # dense_favorite == query; exact_match orthogonal
        [SparseVector(indices=[5], values=[1.0]), query_sparse],  # only exact_match overlaps
    )

    embedder = _ScriptedEmbedder(
        dense={query: query_dense}, sparse={query: query_sparse}, dimensions=_DIM
    )
    retriever = VectorRetriever(
        vector_store=store,
        embedder=embedder,
        cache=FakeCache(),
        ledger=FakeDocumentLedger(),
        prefetch_limit=vector_settings.retrieval.vector.prefetch_limit,
        rrf_k=vector_settings.retrieval.fusion.rrf_k,
        cache_prefix=vector_settings.cache.retrieval.prefix,
        cache_ttl_s=vector_settings.cache.retrieval.ttl_s,
        cache_enabled=vector_settings.cache.retrieval.enabled,
        config_hash=vector_settings.config_hash,
    )

    results = await retriever.retrieve(query, top_k=5)

    assert results[0].chunk.chunk_id == exact_match.chunk_id
    assert results[0].origin == "vector"
    assert results[0].rank == 1


async def test_cache_invalidated_by_corpus_version(vector_settings: Settings, redis_client) -> None:
    """query -> ingest -> same query -> cache miss, fresh result (BUILD_ORDER gate). `ingest`
    is stood in for by bumping the ledger's corpus_version directly — that IS the observable
    side effect `resolve_entities` produces on a real ingest (see that task's own
    `bump_corpus_version()` call) — against a REAL Redis so the cache-key/TTL wiring is proven
    against the real port, not `FakeCache`'s in-memory dict.
    """
    vector_store = FakeVectorStore()
    chunk = make_chunk("cache test chunk")
    await vector_store.upsert_chunks([chunk], [[0.0] * 4], [SparseVector(indices=[], values=[])])

    ledger = FakeDocumentLedger()
    cache = RedisCache(redis_client)

    class _ConstantEmbedder:
        dimensions = 4

        async def embed_dense(self, texts, *, is_query=False):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        async def embed_sparse(self, texts):
            return [SparseVector(indices=[], values=[]) for _ in texts]

    retriever = VectorRetriever(
        vector_store=vector_store,
        embedder=_ConstantEmbedder(),
        cache=cache,
        ledger=ledger,
        prefetch_limit=vector_settings.retrieval.vector.prefetch_limit,
        rrf_k=vector_settings.retrieval.fusion.rrf_k,
        cache_prefix=vector_settings.cache.retrieval.prefix,
        cache_ttl_s=vector_settings.cache.retrieval.ttl_s,
        cache_enabled=vector_settings.cache.retrieval.enabled,
        config_hash=vector_settings.config_hash,
    )

    await retriever.retrieve("same query", top_k=5)
    assert vector_store.hybrid_search_calls == 1

    await retriever.retrieve("same query", top_k=5)
    assert vector_store.hybrid_search_calls == 1  # cache hit -- no second store call

    await ledger.bump_corpus_version()  # stands in for a real ingest

    await retriever.retrieve("same query", top_k=5)
    assert vector_store.hybrid_search_calls == 2  # corpus_version changed -> cache miss
