"""FastEmbedEmbedder — implements `core.ports.Embedder`. See BLUEPRINT §5.1.

Local ONNX embeddings, CPU-only, no network calls at inference time (fastembed downloads model
weights once on first use and caches them on disk). Must stay free per `config.example.yaml`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, ClassVar

from graphrag.core.models import SparseVector

if TYPE_CHECKING:
    from fastembed import SparseTextEmbedding, TextEmbedding

    from graphrag.config.schema import EmbeddingSection
    from graphrag.core.ports import Cache

_log = logging.getLogger(__name__)

# BLUEPRINT §5.1's constructor signature is `__init__(self, settings: EmbeddingSection,
# cache: Cache | None)` — it does not thread `CacheSection.embedding.ttl_s` through, so there is
# no config-driven value available here. This constant mirrors config.example.yaml's documented
# default (30 days) rather than reading it from config. Flagged as a spec gap in the BO-04 report.
_CACHE_TTL_S = 2_592_000


class _ModelCache:
    """Process-wide, thread-safe lazy singleton for the (heavy) ONNX model objects.

    fastembed model construction loads ONNX weights from disk/network; doing that once per
    process — not once per `FastEmbedEmbedder` instance — is what "loading is thread-safe" in
    the BLUEPRINT contract means. Keyed by model name so dense and sparse models coexist.
    """

    _lock = threading.Lock()
    _dense: ClassVar[dict[str, TextEmbedding]] = {}
    _sparse: ClassVar[dict[str, SparseTextEmbedding]] = {}

    @classmethod
    def dense(cls, model_name: str) -> TextEmbedding:
        if model_name not in cls._dense:
            with cls._lock:
                if model_name not in cls._dense:
                    from fastembed import TextEmbedding

                    cls._dense[model_name] = TextEmbedding(model_name=model_name)
        return cls._dense[model_name]

    @classmethod
    def sparse(cls, model_name: str) -> SparseTextEmbedding:
        if model_name not in cls._sparse:
            with cls._lock:
                if model_name not in cls._sparse:
                    from fastembed import SparseTextEmbedding

                    cls._sparse[model_name] = SparseTextEmbedding(model_name=model_name)
        return cls._sparse[model_name]


def _cache_key(model: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"emb:{model}:{digest}"


def _to_sparse_vector(raw_indices: Iterable[int], raw_values: Iterable[float]) -> SparseVector:
    """`SparseVector` requires unique, ascending indices (BLUEPRINT §3.2); fastembed's BM25
    output is neither — it's hash-bucket order. Colliding indices (a hashing-trick artifact)
    are summed, which is the correct term-frequency semantics for two stems landing in the same
    bucket, not a bug to paper over."""
    counts: dict[int, float] = {}
    for index, value in zip(raw_indices, raw_values, strict=True):
        idx = int(index)
        counts[idx] = counts.get(idx, 0.0) + float(value)
    ordered = sorted(counts)
    return SparseVector(indices=ordered, values=[counts[i] for i in ordered])


class FastEmbedEmbedder:
    """Local ONNX embeddings. Implements `Embedder`. See BLUEPRINT §5.1.

    Contract:
        - Models load lazily on first use, once per process; loading is thread-safe (see
          `_ModelCache`).
        - `embed_dense` prepends `embedding.dense.query_prefix` when `is_query=True` and never
          otherwise. Prepending happens HERE, via plain `embed()` — never `query_embed()`. For
          ONNX models such as bge-small-en-v1.5, fastembed 0.8.0's `query_embed()` falls through
          to the base implementation, which just calls `embed()` and applies NO prefix; relying
          on it would silently ship unprefixed queries. See `test_adapter_prepends_prefix_itself`.
        - Batches internally at `embedding.dense.batch_size` regardless of input size (passed
          straight through to fastembed's own `batch_size` parameter).
        - Runs the blocking, CPU-bound encode in a thread via `asyncio.to_thread`.
        - Results are cache-read/written through the injected `Cache` when not None, keyed
          `f"emb:{model}:{sha256(text)}"` — the SAME cache instance the caller passes governs
          both dense and sparse (model name disambiguates the two).
        - `embed_sparse` returns TERM-FREQUENCY vectors only; IDF is applied by Qdrant.
    """

    def __init__(self, settings: EmbeddingSection, cache: Cache | None) -> None:
        self._settings = settings
        self._cache = cache

    @property
    def dimensions(self) -> int:
        return self._settings.dense.dimensions

    async def embed_dense(
        self, texts: Sequence[str], *, is_query: bool = False
    ) -> list[list[float]]:
        if not texts:
            return []
        prefix = self._settings.dense.query_prefix if is_query else ""
        prepared = [f"{prefix}{text}" for text in texts]
        return await self._embed_cached(
            model=self._settings.dense.model,
            prepared=prepared,
            encode=self._encode_dense,
            serialize=lambda vector: json.dumps(vector).encode("utf-8"),
            deserialize=lambda raw: list(json.loads(raw)),
        )

    async def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]:
        if not texts:
            return []
        return await self._embed_cached(
            model=self._settings.sparse.model,
            prepared=list(texts),
            encode=self._encode_sparse,
            serialize=lambda sv: json.dumps({"indices": sv.indices, "values": sv.values}).encode(
                "utf-8"
            ),
            deserialize=lambda raw: SparseVector(**json.loads(raw)),
        )

    async def _embed_cached[T](
        self,
        *,
        model: str,
        prepared: list[str],
        encode: Callable[[list[str]], list[T]],
        serialize: Callable[[T], bytes],
        deserialize: Callable[[bytes], T],
    ) -> list[T]:
        results: list[T | None] = [None] * len(prepared)
        misses: list[int] = []
        for i, text in enumerate(prepared):
            cached = await self._cache_get(model, text, deserialize)
            if cached is not None:
                results[i] = cached
            else:
                misses.append(i)

        if misses:
            encoded = await asyncio.to_thread(encode, [prepared[i] for i in misses])
            for i, value in zip(misses, encoded, strict=True):
                results[i] = value
                await self._cache_set(model, prepared[i], value, serialize)

        return [r for r in results if r is not None]  # every slot is filled by construction

    async def _cache_get[T](
        self, model: str, text: str, deserialize: Callable[[bytes], T]
    ) -> T | None:
        if self._cache is None:
            return None
        raw = await self._cache.get(_cache_key(model, text))
        if raw is None:
            return None
        return deserialize(raw)

    async def _cache_set[T](
        self, model: str, text: str, value: T, serialize: Callable[[T], bytes]
    ) -> None:
        if self._cache is None:
            return
        await self._cache.set(_cache_key(model, text), serialize(value), ttl_s=_CACHE_TTL_S)

    def _encode_dense(self, texts: list[str]) -> list[list[float]]:
        model = _ModelCache.dense(self._settings.dense.model)
        vectors = model.embed(texts, batch_size=self._settings.dense.batch_size)
        return [[float(x) for x in vector] for vector in vectors]

    def _encode_sparse(self, texts: list[str]) -> list[SparseVector]:
        model = _ModelCache.sparse(self._settings.sparse.model)
        embeddings = model.embed(texts)
        return [_to_sparse_vector(e.indices, e.values) for e in embeddings]
