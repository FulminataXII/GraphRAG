"""`ResolutionService` against real Qdrant. See BLUEPRINT §6.2 / BUILD_ORDER BO-07.

Uses `FakeEmbedder` (BO-04 covers embedding correctness, not this BO) — this is safe here
specifically because both gate tests below only exercise mentions that share an identical
NORMALIZED name (`normalize_entity_name` groups them before any embedding-driven scoring ever
runs), so the embedding values themselves are never load-bearing for the assertions.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from qdrant_client import AsyncQdrantClient

from graphrag.adapters.qdrant_store import QdrantVectorStore
from graphrag.adapters.telemetry.metrics import Metrics
from graphrag.config.settings import Settings
from graphrag.core.models import EntityType, Mention
from graphrag.services.resolution.blocking import Blocker
from graphrag.services.resolution.service import ResolutionService
from tests.factories import make_metrics
from tests.fakes import FakeEmbedder
from tests.integration import namespaces as ns
from tests.integration.conftest import drop_collections
from tests.unit._settings_helpers import set_required_secrets

pytestmark = pytest.mark.integration

_QDRANT_URL = "http://localhost:6333"
_DIM = 4


@pytest.fixture
def resolution_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
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
async def vector_store(
    qdrant_client: AsyncQdrantClient, resolution_settings: Settings
) -> AsyncIterator[QdrantVectorStore]:
    store = QdrantVectorStore(qdrant_client, resolution_settings)
    await store.ensure_collections()
    yield store
    await drop_collections(qdrant_client, resolution_settings.resolution.collection)


@pytest.fixture
def metrics() -> Metrics:
    return make_metrics()[0]


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder(dimensions=_DIM)


@pytest.fixture
def resolution_service(
    vector_store: QdrantVectorStore,
    resolution_settings: Settings,
    metrics: Metrics,
    embedder: FakeEmbedder,
) -> ResolutionService:
    blocker = Blocker(vector_store=vector_store, resolution=resolution_settings.resolution)
    return ResolutionService(
        blocker=blocker,
        embedder=embedder,
        resolution=resolution_settings.resolution,
        metrics=metrics,
    )


def _mention(surface: str, entity_type: EntityType = EntityType.ORG) -> Mention:
    return Mention(
        surface=surface,
        type=entity_type,
        chunk_id=uuid4(),
        char_start=0,
        char_end=len(surface),
        confidence=0.9,
    )


async def _persist(
    service: ResolutionService,
    vector_store: QdrantVectorStore,
    embedder: FakeEmbedder,
    mentions: list[Mention],
):
    result = await service.resolve(mentions)
    if result.entities:
        vectors = await embedder.embed_dense([entity.name_normalized for entity in result.entities])
        await vector_store.upsert_entities(result.entities, vectors)
    return result


async def test_three_variants_merge(
    resolution_service: ResolutionService, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    """`"Acme Corp." / "ACME Corporation" / "Acme"` -> one canonical_id, two alias edges."""
    mentions = [
        _mention("Acme Corp."),
        _mention("ACME Corporation"),
        _mention("Acme"),
    ]
    result = await _persist(resolution_service, vector_store, embedder, mentions)

    assert len(result.entities) == 1
    assert len(result.aliases) == 2
    assert all(edge.canonical_id == result.entities[0].canonical_id for edge in result.aliases)


async def test_resolution_idempotent(
    resolution_service: ResolutionService, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    """Re-running resolve() (as a retried/duplicate `resolve_entities` job would) over the SAME
    mentions produces zero new canonical_ids and zero new aliases."""
    mentions = [
        _mention("Acme Corp."),
        _mention("ACME Corporation"),
        _mention("Acme"),
    ]
    first = await _persist(resolution_service, vector_store, embedder, mentions)
    first_ids = {entity.canonical_id for entity in first.entities}
    first_alias_ids = {edge.alias_id for edge in first.aliases}

    second = await _persist(resolution_service, vector_store, embedder, mentions)
    second_ids = {entity.canonical_id for entity in second.entities}
    second_alias_ids = {edge.alias_id for edge in second.aliases}

    assert second_ids == first_ids  # zero NEW canonical_ids
    assert second_alias_ids == first_alias_ids  # zero NEW aliases
