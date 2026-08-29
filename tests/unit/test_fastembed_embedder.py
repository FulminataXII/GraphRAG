"""`FastEmbedEmbedder` unit tests. See BLUEPRINT §5.1 / BUILD_ORDER BO-04.

Runs the real bge-small-en-v1.5 / Qdrant-bm25 ONNX models (fastembed downloads and caches them
on first use — no docker-compose dependency, so these are `unit`, not `integration`, per
BUILD_ORDER's own tagging). Tests that don't need real inference (batching, cache-hit) monkeypatch
`_ModelCache` so they never touch the network.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from graphrag.adapters import fastembed_embedder as mod
from graphrag.adapters.fastembed_embedder import FastEmbedEmbedder
from graphrag.config.settings import Settings
from tests.fakes import FakeCache


async def test_query_prefix_applied_only_for_queries(settings: Settings) -> None:
    """The prefix appears on query text and never on indexed text.

    `embed_dense(is_query=True)` on "hello" must equal `embed_dense(is_query=False)` on the
    already-prefixed text, and differ from `embed_dense(is_query=False)` on the bare text —
    proving the prefix is applied exactly once, exactly for queries.
    """
    embedder = FastEmbedEmbedder(settings.embedding, cache=None)
    prefix = settings.embedding.dense.query_prefix

    [query_vec] = await embedder.embed_dense(["hello"], is_query=True)
    [doc_vec] = await embedder.embed_dense(["hello"], is_query=False)
    [manually_prefixed_vec] = await embedder.embed_dense([f"{prefix}hello"], is_query=False)

    assert query_vec != doc_vec
    assert query_vec == manually_prefixed_vec


async def test_adapter_prepends_prefix_itself(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`embed_dense(is_query=True)` must never call fastembed's `query_embed()` — see
    BLUEPRINT §5.1: for ONNX models like bge-small-en-v1.5, fastembed 0.8.0's `query_embed()`
    applies no prefix, so relying on it would silently ship unprefixed queries."""
    from fastembed import TextEmbedding

    def _must_not_be_called(self: object, *args: object, **kwargs: object) -> None:
        raise AssertionError("query_embed() must never be called by FastEmbedEmbedder")

    monkeypatch.setattr(TextEmbedding, "query_embed", _must_not_be_called)

    embedder = FastEmbedEmbedder(settings.embedding, cache=None)
    [vector] = await embedder.embed_dense(["hello"], is_query=True)

    assert len(vector) == settings.embedding.dense.dimensions


async def test_embedder_batches_at_configured_size(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`batch_size` is forwarded straight through to fastembed's own batching, regardless of
    input size — no network needed, since the model itself is replaced with a recorder."""

    class _RecordingModel:
        def __init__(self) -> None:
            self.batch_sizes: list[int | None] = []

        def embed(
            self, texts: list[str], batch_size: int | None = None, **_: object
        ) -> list[list[float]]:
            self.batch_sizes.append(batch_size)
            return [[0.0, 1.0] for _ in texts]

    recorder = _RecordingModel()
    monkeypatch.setattr(mod._ModelCache, "dense", classmethod(lambda cls, name: recorder))

    embedder = FastEmbedEmbedder(settings.embedding, cache=None)
    await embedder.embed_dense(["a", "b", "c"])

    assert recorder.batch_sizes == [settings.embedding.dense.batch_size]


async def test_embedding_cache_hit_skips_encode(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-populated cache entry is returned without ever loading the model."""

    def _must_not_load(cls: type, name: str) -> None:
        raise AssertionError("model must not load on a cache hit")

    monkeypatch.setattr(mod._ModelCache, "dense", classmethod(_must_not_load))

    cache = FakeCache()
    model = settings.embedding.dense.model
    text = "hello"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    await cache.set(f"emb:{model}:{digest}", json.dumps([1.0, 2.0, 3.0]).encode("utf-8"), ttl_s=60)

    embedder = FastEmbedEmbedder(settings.embedding, cache)
    result = await embedder.embed_dense([text])

    assert result == [[1.0, 2.0, 3.0]]


async def test_embed_dense_dimensions_match_config(settings: Settings) -> None:
    embedder = FastEmbedEmbedder(settings.embedding, cache=None)
    [vector] = await embedder.embed_dense(["a real sentence to embed for real"])

    assert len(vector) == settings.embedding.dense.dimensions == embedder.dimensions


async def test_embed_dense_empty_input_returns_empty_list(settings: Settings) -> None:
    embedder = FastEmbedEmbedder(settings.embedding, cache=None)
    assert await embedder.embed_dense([]) == []


async def test_embed_sparse_returns_term_frequency_vectors(settings: Settings) -> None:
    embedder = FastEmbedEmbedder(settings.embedding, cache=None)
    [sparse] = await embedder.embed_sparse(["the quick brown fox jumps over the lazy dog"])

    assert len(sparse.indices) == len(sparse.values)
    assert len(sparse.indices) > 0
    assert sparse.indices == sorted(sparse.indices)


async def test_embed_sparse_empty_input_returns_empty_list(settings: Settings) -> None:
    embedder = FastEmbedEmbedder(settings.embedding, cache=None)
    assert await embedder.embed_sparse([]) == []
