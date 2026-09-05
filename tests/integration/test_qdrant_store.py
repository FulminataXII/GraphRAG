"""`QdrantVectorStore` against a real Qdrant. See BLUEPRINT §5.2 / BUILD_ORDER BO-04.

Every test gets its own chunks/entities collection pair under this run's namespace (see
`qdrant_settings` / tests/integration/namespaces.py), so tests never collide with each other,
with a real corpus, or depend on run order — despite
BUILD_ORDER's note that `test_collection_created_with_idf_modifier` should "run first", that's
about a human's manual debugging order, not a pytest ordering requirement this suite relies on.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from qdrant_client import AsyncQdrantClient, models

from graphrag.adapters.qdrant_store import QdrantVectorStore
from graphrag.config.settings import Settings
from graphrag.core.errors import ConflictError
from tests.factories import make_chunk, make_source_ref
from tests.integration import namespaces as ns
from tests.integration.conftest import drop_collections
from tests.unit._settings_helpers import set_required_secrets

pytestmark = pytest.mark.integration

_QDRANT_URL = "http://localhost:6333"
_DIM = 4  # override embedding.dense.dimensions to keep hand-crafted vectors small/legible


@pytest.fixture
def qdrant_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Real `Settings()` (APP_ENV defaults to "local", so `stores.qdrant.url` resolves to
    localhost — see tests/integration/conftest.py's docstring), namespaced to this run and this
    test, with the dense dimension shrunk to keep hand-crafted vectors legible."""
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
    qdrant_client: AsyncQdrantClient, qdrant_settings: Settings
) -> AsyncIterator[QdrantVectorStore]:
    yield QdrantVectorStore(qdrant_client, qdrant_settings)
    await drop_collections(
        qdrant_client,
        qdrant_settings.retrieval.vector.collection,
        qdrant_settings.resolution.collection,
    )


def _sparse(indices: list[int], values: list[float]) -> models.SparseVector:
    return models.SparseVector(indices=indices, values=values)


async def test_collection_created_with_idf_modifier(
    store: QdrantVectorStore, qdrant_client: AsyncQdrantClient, qdrant_settings: Settings
) -> None:
    await store.ensure_collections()

    info = await qdrant_client.get_collection(qdrant_settings.retrieval.vector.collection)
    sparse_vectors = info.config.params.sparse_vectors
    assert sparse_vectors is not None
    assert sparse_vectors["bm25"].modifier == models.Modifier.IDF


async def test_existing_collection_without_idf_raises(
    store: QdrantVectorStore, qdrant_client: AsyncQdrantClient, qdrant_settings: Settings
) -> None:
    name = qdrant_settings.retrieval.vector.collection
    await qdrant_client.create_collection(
        collection_name=name,
        vectors_config={"dense": models.VectorParams(size=_DIM, distance=models.Distance.COSINE)},
        sparse_vectors_config={"bm25": models.SparseVectorParams()},  # no IDF modifier
    )

    with pytest.raises(ConflictError, match="IDF modifier"):
        await store.ensure_collections()


async def test_ensure_collections_idempotent(
    store: QdrantVectorStore, qdrant_client: AsyncQdrantClient, qdrant_settings: Settings
) -> None:
    await store.ensure_collections()
    await store.ensure_collections()  # must not raise, must not recreate

    info = await qdrant_client.get_collection(qdrant_settings.retrieval.vector.collection)
    assert info.config.params.sparse_vectors["bm25"].modifier == models.Modifier.IDF


async def test_payload_indexes_created(
    store: QdrantVectorStore, qdrant_client: AsyncQdrantClient, qdrant_settings: Settings
) -> None:
    await store.ensure_collections()

    info = await qdrant_client.get_collection(qdrant_settings.retrieval.vector.collection)
    schema = info.payload_schema

    assert schema["doc_ids"].data_type == models.PayloadSchemaType.KEYWORD
    assert schema["entity_ids"].data_type == models.PayloadSchemaType.KEYWORD
    assert schema["schema_version"].data_type == models.PayloadSchemaType.INTEGER


async def test_doc_ids_filter_uses_index(store: QdrantVectorStore) -> None:
    await store.ensure_collections()
    chunk_a = make_chunk("chunk about apples", sources=[make_source_ref(doc_id="doc-a")])
    chunk_b = make_chunk("chunk about bananas", sources=[make_source_ref(doc_id="doc-b")])
    dense = [1.0, 0.0, 0.0, 0.0]
    sparse = _sparse([1], [1.0])
    await store.upsert_chunks([chunk_a, chunk_b], [dense, dense], [sparse, sparse])

    results = await store.hybrid_search(
        dense=dense,
        sparse=sparse,
        top_k=10,
        prefetch_limit=10,
        rrf_k=60,
        weights=None,
        filters={"doc_ids": ["doc-a"]},
    )

    assert {r.chunk.chunk_id for r in results} == {chunk_a.chunk_id}


async def test_hybrid_uses_rrfquery_with_k(store: QdrantVectorStore) -> None:
    """Two rrf_k values, held against the same fixed dense/sparse rank conflict, produce
    different result orders — proving `rrf_k` actually reaches Qdrant's RRF. An
    order-INVARIANT result across k would mean a parameterless `FusionQuery` is in use instead
    (BLUEPRINT §5.2) and the knob is dead. Values verified empirically against a live instance
    before being pinned here."""
    await store.ensure_collections()
    dense_vectors = {
        "P0": [1.0, 0.0, 0.0, 0.0],
        "P1": [0.9, 0.1, 0.0, 0.0],
        "P2": [0.1, 0.9, 0.0, 0.0],
        "P3": [0.0, 1.0, 0.0, 0.0],
        "P4": [0.0, 0.0, 1.0, 0.0],
    }
    sparse_vectors = {
        "P0": _sparse([5], [1.0]),
        "P1": _sparse([1], [0.2]),
        "P2": _sparse([1], [0.5]),
        "P3": _sparse([1], [0.8]),
        "P4": _sparse([1], [1.0]),
    }
    chunks = {
        label: make_chunk(f"chunk {label}", sources=[make_source_ref(doc_id=f"doc-{label}")])
        for label in dense_vectors
    }
    await store.upsert_chunks(
        list(chunks.values()),
        [dense_vectors[label] for label in chunks],
        [sparse_vectors[label] for label in chunks],
    )

    query_dense = [1.0, 0.0, 0.0, 0.0]
    query_sparse = _sparse([1], [1.0])

    order_k1 = [
        r.chunk.chunk_id
        for r in await store.hybrid_search(
            dense=query_dense,
            sparse=query_sparse,
            top_k=5,
            prefetch_limit=10,
            rrf_k=1,
            weights=None,
        )
    ]
    order_k1000 = [
        r.chunk.chunk_id
        for r in await store.hybrid_search(
            dense=query_dense,
            sparse=query_sparse,
            top_k=5,
            prefetch_limit=10,
            rrf_k=1000,
            weights=None,
        )
    ]

    assert order_k1 != order_k1000


async def test_hybrid_handles_empty_sparse(store: QdrantVectorStore) -> None:
    await store.ensure_collections()
    chunk = make_chunk("dense-only chunk", sources=[make_source_ref(doc_id="doc-dense")])
    await store.upsert_chunks([chunk], [[1.0, 0.0, 0.0, 0.0]], [_sparse([], [])])

    results = await store.hybrid_search(
        dense=[1.0, 0.0, 0.0, 0.0],
        sparse=_sparse([], []),
        top_k=5,
        prefetch_limit=10,
        rrf_k=60,
        weights=None,
    )

    assert [r.chunk.chunk_id for r in results] == [chunk.chunk_id]


async def test_sparse_beats_dense_on_rare_token(store: QdrantVectorStore) -> None:
    """A chunk with a poor dense match but an exact sparse/rare-token match outranks a chunk
    with a perfect dense match and no sparse overlap — the symptom that disappears if the IDF
    modifier isn't actually wired in (BLUEPRINT §5.2's note on BO-04's regression risk)."""
    await store.ensure_collections()
    dense_favorite = make_chunk(
        "dense favorite, no shared vocabulary", sources=[make_source_ref(doc_id="doc-dense")]
    )
    rare_term_match = make_chunk(
        "chunk containing the rare token", sources=[make_source_ref(doc_id="doc-rare")]
    )
    query_dense = [1.0, 0.0, 0.0, 0.0]
    query_sparse = _sparse([1], [1.0])

    await store.upsert_chunks(
        [dense_favorite, rare_term_match],
        [query_dense, [0.0, 0.0, 1.0, 0.0]],  # dense_favorite == query; rare_term_match ⟂ query
        [_sparse([5], [1.0]), query_sparse],  # dense_favorite: no overlap; rare: exact match
    )

    results = await store.hybrid_search(
        dense=query_dense,
        sparse=query_sparse,
        top_k=5,
        prefetch_limit=10,
        rrf_k=60,
        weights=None,
    )

    assert results[0].chunk.chunk_id == rare_term_match.chunk_id


async def test_set_sources_overwrites_not_appends(store: QdrantVectorStore) -> None:
    await store.ensure_collections()
    chunk = make_chunk("shared chunk", sources=[make_source_ref(doc_id="doc-1")])
    await store.upsert_chunks([chunk], [[1.0, 0.0, 0.0, 0.0]], [_sparse([1], [1.0])])

    await store.set_sources(chunk.chunk_id, [make_source_ref(doc_id="doc-2")])

    [fetched] = await store.get_chunks([chunk.chunk_id])
    assert [s.doc_id for s in fetched.sources] == ["doc-2"]


async def test_entity_upsert_and_search_roundtrip(store: QdrantVectorStore) -> None:
    """`ensure_collections()`'s "entity methods" scope (BUILD_ORDER BO-04): the entities
    collection isn't detailed in BLUEPRINT §5.2's contract block — see the module docstring in
    `qdrant_store.py` for the inferred shape this exercises."""
    from graphrag.core.models import Entity, EntityType

    entity = Entity(
        canonical_id=uuid.uuid4(),
        name="Acme Corp",
        name_normalized="acme",
        type=EntityType.ORG,
        aliases=["Acme"],
        mention_count=3,
    )
    await store.ensure_collections()
    await store.upsert_entities([entity], [[1.0, 0.0, 0.0, 0.0]])

    results = await store.search_entities([1.0, 0.0, 0.0, 0.0], top_k=5, entity_type=None)

    assert [e.canonical_id for e, _score in results] == [entity.canonical_id]
