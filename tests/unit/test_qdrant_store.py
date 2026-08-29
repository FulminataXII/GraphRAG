"""`QdrantVectorStore` unit tests. See BLUEPRINT §5.2 / BUILD_ORDER BO-04.

Behavior that needs a real Qdrant (collection creation, hybrid search, filters) lives in
`tests/integration/test_qdrant_store.py`. What's here needs no backend at all: error wrapping
(via a hand-rolled client stand-in, no mocks) and a static source scan.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from graphrag.adapters.qdrant_store import QdrantVectorStore, _build_filter, _doc_ids
from graphrag.config.settings import Settings
from graphrag.core.errors import RetrievalBackendUnavailable
from tests.factories import make_source_ref


class _ExplodingQdrantClient:
    """Every method a `QdrantVectorStore` call might reach raises like a dead connection."""

    async def collection_exists(self, *args: object, **kwargs: object) -> bool:
        raise ConnectionError("qdrant unreachable")

    async def get_collection(self, *args: object, **kwargs: object) -> object:
        raise ConnectionError("qdrant unreachable")

    async def retrieve(self, *args: object, **kwargs: object) -> list[object]:
        raise ConnectionError("qdrant unreachable")

    async def upsert(self, *args: object, **kwargs: object) -> object:
        raise ConnectionError("qdrant unreachable")

    async def delete(self, *args: object, **kwargs: object) -> object:
        raise ConnectionError("qdrant unreachable")

    async def set_payload(self, *args: object, **kwargs: object) -> object:
        raise ConnectionError("qdrant unreachable")

    async def query_points(self, *args: object, **kwargs: object) -> object:
        raise ConnectionError("qdrant unreachable")

    async def get_collections(self, *args: object, **kwargs: object) -> object:
        raise ConnectionError("qdrant unreachable")


async def test_store_errors_wrap_to_retrieval_backend_unavailable(settings: Settings) -> None:
    store = QdrantVectorStore(_ExplodingQdrantClient(), settings)  # type: ignore[arg-type]

    with pytest.raises(RetrievalBackendUnavailable):
        await store.get_chunks([uuid4()])
    with pytest.raises(RetrievalBackendUnavailable):
        await store.ensure_collections()
    with pytest.raises(RetrievalBackendUnavailable):
        await store.set_sources(uuid4(), [make_source_ref()])


async def test_health_returns_false_on_backend_error(settings: Settings) -> None:
    store = QdrantVectorStore(_ExplodingQdrantClient(), settings)  # type: ignore[arg-type]

    assert await store.health() is False


async def test_get_chunks_empty_input_short_circuits(settings: Settings) -> None:
    """No client call at all for an empty ID list — `_ExplodingQdrantClient` would raise if
    `retrieve()` were reached."""
    store = QdrantVectorStore(_ExplodingQdrantClient(), settings)  # type: ignore[arg-type]

    assert await store.get_chunks([]) == []


def test_doc_ids_deduplicates_preserving_first_occurrence() -> None:
    refs = [
        make_source_ref(doc_id="doc-a"),
        make_source_ref(doc_id="doc-b"),
        make_source_ref(doc_id="doc-a"),
    ]

    assert _doc_ids(refs) == ["doc-a", "doc-b"]


def test_build_filter_none_for_empty_or_missing() -> None:
    assert _build_filter(None) is None
    assert _build_filter({}) is None


def test_no_nested_condition_anywhere_in_codebase() -> None:
    """BLUEPRINT §5.2: filter on the flat `doc_ids` keyword array, never Qdrant's
    `NestedCondition` — nested-array payload filtering is both unreliable to index and orders
    of magnitude slower than a flat keyword match."""
    graphrag_root = Path(__file__).resolve().parents[2] / "graphrag"
    offenders = [
        path
        for path in graphrag_root.rglob("*.py")
        if "NestedCondition" in path.read_text(encoding="utf-8")
    ]

    assert offenders == [], f"NestedCondition used in: {offenders}"
