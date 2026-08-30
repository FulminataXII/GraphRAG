"""`Container` / `ReadyzProber` unit tests. See BLUEPRINT §7.1.

`ReadyzProber` is deliberately dependency-free (plain probe callables in, `{name: healthy}` out),
and `Container.aclose()`'s ordering only depends on the `closers` list — both are exercised here
with hand-rolled fakes, no real Postgres/Redis/Qdrant/Neo4j/LiteLLM.
"""

from __future__ import annotations

import asyncio

import pytest

from graphrag.apps.api.main import Container, ReadyzProber, _assert_embedding_dimensions
from graphrag.core.errors import ConflictError
from tests.factories import make_metrics
from tests.fakes import (
    FakeCache,
    FakeDocumentLedger,
    FakeEmbedder,
    FakeJobQueue,
    FakeSourceRegistry,
)


async def test_healthz_performs_no_io() -> None:
    from graphrag.apps.api.routers.health import healthz

    assert await healthz() == {"status": "ok"}


async def test_readyz_result_cached() -> None:
    call_count = 0

    async def _probe() -> bool:
        nonlocal call_count
        call_count += 1
        return True

    prober = ReadyzProber({"postgres": _probe}, cache_s=5, timeout_s=1)

    for _ in range(10):
        result = await prober.check()

    assert result == {"postgres": True}
    assert call_count == 1  # 10 calls within cache_s -> exactly one probe round


async def test_readyz_bounded_by_deadline() -> None:
    async def _hangs_forever() -> bool:
        await asyncio.sleep(60)
        return True  # pragma: no cover - never reached

    async def _fast() -> bool:
        return True

    prober = ReadyzProber({"neo4j": _hangs_forever, "postgres": _fast}, cache_s=5, timeout_s=0.05)

    result = await asyncio.wait_for(prober.check(), timeout=1.0)

    assert result == {"neo4j": False, "postgres": True}


async def test_readyz_probe_exception_reports_unhealthy_not_raise() -> None:
    async def _raises() -> bool:
        raise ConnectionError("backend is down")

    prober = ReadyzProber({"qdrant": _raises}, cache_s=5, timeout_s=1)

    assert await prober.check() == {"qdrant": False}


async def test_container_asserts_embedding_dimensions() -> None:
    """A model whose real width differs from `embedding.dense.dimensions` raises in
    `Container.create()`, before `ensure_collections()`. Exercised directly against the
    extracted assertion (not a full `Container.create()`, which needs real Postgres/Redis/
    Qdrant) using `FakeEmbedder`, whose `embed_dense` produces vectors of exactly its
    configured `.dimensions` width — a real, deterministic mismatch, not a mock."""
    embedder = FakeEmbedder(dimensions=8)

    with pytest.raises(ConflictError):
        await _assert_embedding_dimensions(embedder, expected=384)

    await _assert_embedding_dimensions(embedder, expected=8)  # matching width: no raise


async def test_lifespan_closes_pools_in_reverse() -> None:
    order: list[str] = []

    async def _close(name: str) -> None:
        order.append(name)

    closers = [
        ("postgres", lambda: _close("postgres")),
        ("redis", lambda: _close("redis")),
        ("arq", lambda: _close("arq")),
    ]

    container = Container(
        settings=None,  # type: ignore[arg-type]  # unused by aclose()
        ledger=FakeDocumentLedger(),
        sources=FakeSourceRegistry(),
        cache=FakeCache(),
        job_queue=FakeJobQueue(),
        readyz_prober=ReadyzProber({}, cache_s=5, timeout_s=1),
        metrics=make_metrics()[0],
        closers=closers,
    )

    await container.aclose()

    assert order == ["arq", "redis", "postgres"]


async def test_aclose_continues_past_a_failing_closer() -> None:
    order: list[str] = []

    async def _ok(name: str) -> None:
        order.append(name)

    async def _fails() -> None:
        raise RuntimeError("close failed")

    closers = [("a", lambda: _ok("a")), ("b", _fails), ("c", lambda: _ok("c"))]
    container = Container(
        settings=None,  # type: ignore[arg-type]
        ledger=FakeDocumentLedger(),
        sources=FakeSourceRegistry(),
        cache=FakeCache(),
        job_queue=FakeJobQueue(),
        readyz_prober=ReadyzProber({}, cache_s=5, timeout_s=1),
        metrics=make_metrics()[0],
        closers=closers,
    )

    await container.aclose()  # must not raise

    assert order == ["c", "a"]  # b failed but didn't block a's cleanup
