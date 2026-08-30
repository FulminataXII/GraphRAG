"""Container, lifespan, create_app. See BLUEPRINT §7.1.

BO-04 scope note: `Container` is the composition root for every port in `core.ports`. As of
BO-04, `ledger`, `sources`, `cache`, `job_queue` (BO-03), `vector_store`, and `embedder` (BO-04)
all have real adapters; `graph_store` and `llm_client` stay `None` until BO-06/08 land theirs —
nothing in this BO calls them.

Per BLUEPRINT §7.1's Container contract, `create()` now also constructs `FastEmbedEmbedder` and
`QdrantVectorStore`, asserts the embedder's real output width matches
`embedding.dense.dimensions` (see `_assert_embedding_dimensions` — a wrong-width model must
raise here, before a Qdrant collection of the wrong size gets created), and calls
`vector_store.ensure_collections()`. A Qdrant failure at startup is therefore no longer
survivable the way BO-03 described it: this BO's `VectorStore` is real and required.

`readyz` still needs Neo4j reachable before BO-08 lands its real adapter (ARCHITECTURE §2.1's
health contract). Building `GraphStore` early to satisfy that would be building ahead of the
current BO (BO-08 owns `ensure_schema()` and the business logic around it). Instead, `readyz`'s
Neo4j probe uses the driver directly — `verify_connectivity()` — which only needs "is this
reachable", not the port's business methods. Qdrant has both: a real `vector_store` (used for
actual traffic) AND a separate probe-only client for `readyz`, kept distinct so a slow/degraded
Qdrant shows up in `readyz` without being routed through the same client object real requests
use.

As of BO-06, `llm_client` is real (`LiteLLMClient`, wrapping an `AsyncOpenAI` pointed at the
LiteLLM gateway's OpenAI-compatible endpoint, authenticated with ONLY
`secrets.litellm_virtual_key` — see BLUEPRINT §5.6). Its `readyz` probe calls `llm_client.health()`
directly rather than a bespoke HTTP ping, since the real port method now exists.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from graphrag.adapters.arq_queue import ArqJobQueue
from graphrag.adapters.fastembed_embedder import FastEmbedEmbedder
from graphrag.adapters.litellm_client import LiteLLMClient
from graphrag.adapters.postgres.ledger import PostgresDocumentLedger
from graphrag.adapters.postgres.sources import PostgresSourceRegistry
from graphrag.adapters.qdrant_store import QdrantVectorStore
from graphrag.adapters.redis_cache import RedisCache
from graphrag.adapters.telemetry.logging import configure_logging
from graphrag.adapters.telemetry.metrics import Metrics
from graphrag.adapters.telemetry.middleware import AccessLogMiddleware, CorrelationIdMiddleware
from graphrag.adapters.telemetry.otel import init_telemetry, meter, shutdown_telemetry
from graphrag.apps.api.errors import install_exception_handlers
from graphrag.config.settings import Settings, get_settings
from graphrag.core.errors import ConflictError

if TYPE_CHECKING:
    from graphrag.core.ports import Cache, DocumentLedger, Embedder, LLMClient
    from graphrag.core.ports import GraphStore as GraphStorePort
    from graphrag.core.ports import JobQueue as JobQueuePort
    from graphrag.core.ports import SourceRegistry as SourceRegistryPort
    from graphrag.core.ports import VectorStore as VectorStorePort

_log = logging.getLogger(__name__)

Probe = Callable[[], Awaitable[bool]]
Closer = Callable[[], Awaitable[None]]


async def _assert_embedding_dimensions(embedder: Embedder, *, expected: int) -> None:
    """Raise before a Qdrant collection of the wrong width gets created.

    `config/settings.py`'s cross-section validator explicitly does NOT check this — confirming
    the model's real output width means loading/running it, which is I/O a pure config object
    may not do. `Container.create()` is where that I/O is allowed, so the check lives here,
    before `vector_store.ensure_collections()`. Takes the `Embedder` Protocol (not
    `FastEmbedEmbedder`) so it is unit-testable with `tests.fakes.FakeEmbedder`.
    """
    [probe_vector] = await embedder.embed_dense(["dimension probe"])
    actual = len(probe_vector)
    if actual != expected:
        raise ConflictError(
            f"embedding.dense produced {actual}-dimensional vectors but "
            f"embedding.dense.dimensions is configured as {expected}. Fix the config value to "
            "match the model's real output width before the Qdrant collection is created.",
            details={"actual": actual, "configured": expected},
        )


class ReadyzProber:
    """Caches probe results for `cache_s` seconds; bounds every individual probe by
    `timeout_s` so one hanging backend can't hang or fan-out-amplify the whole check.

    Standalone and dependency-free (plain probe callables in, `{name: healthy}` out) so it is
    unit-testable without any real backend. See BLUEPRINT §7.1's `readyz` contract.
    """

    def __init__(self, probes: dict[str, Probe], *, cache_s: float, timeout_s: float) -> None:
        self._probes = probes
        self._cache_s = cache_s
        self._timeout_s = timeout_s
        self._cached: dict[str, bool] | None = None
        self._cached_at: float | None = None

    async def check(self) -> dict[str, bool]:
        now = asyncio.get_running_loop().time()
        if (
            self._cached is not None
            and self._cached_at is not None
            and now - self._cached_at < self._cache_s
        ):
            return self._cached
        results = await self._run_probes()
        self._cached = results
        self._cached_at = now
        return results

    async def _run_probes(self) -> dict[str, bool]:
        results: dict[str, bool] = {}

        async def run_one(name: str, probe: Probe) -> None:
            try:
                async with asyncio.timeout(self._timeout_s):
                    results[name] = await probe()
            except Exception:
                results[name] = False

        async with asyncio.TaskGroup() as tg:
            for name, probe in self._probes.items():
                tg.create_task(run_one(name, probe))
        return results


class Container:
    """Composition root. Built once in lifespan; owns every adapter's lifecycle.

    Contract (BLUEPRINT §7.1):
        - create(settings) constructs clients, asserts the embedder's real dimensions, calls
          vector_store.ensure_collections(), and returns a ready container. Raises on any
          failure of a backend this BO actually depends on (Postgres, Redis, Qdrant) — the
          process must not start with a half-built container. Neo4j/LiteLLM reachability is NOT
          required at startup (see module docstring): they're probed lazily by readyz(), and
          the whole point of readyz/degraded-mode is that the app starts and serves even when
          one of them is down.
        - aclose() closes pools in reverse construction order.
        - The worker builds its own Container in arq's on_startup (BO-05). Same class, same
          settings.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        ledger: DocumentLedger,
        sources: SourceRegistryPort,
        cache: Cache,
        job_queue: JobQueuePort,
        readyz_prober: ReadyzProber,
        metrics: Metrics,
        vector_store: VectorStorePort | None = None,
        graph_store: GraphStorePort | None = None,
        embedder: Embedder | None = None,
        llm_client: LLMClient | None = None,
        closers: list[tuple[str, Closer]] | None = None,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.sources = sources
        self.cache = cache
        self.job_queue = job_queue
        self.vector_store = vector_store
        self.graph_store = graph_store
        self.embedder = embedder
        self.llm_client = llm_client
        self.metrics = metrics
        self._readyz_prober = readyz_prober
        self._closers = closers or []

    @classmethod
    async def create(cls, settings: Settings) -> Container:
        closers: list[tuple[str, Closer]] = []

        pg_dsn = settings.secrets.postgres_dsn.get_secret_value()
        if pg_dsn.startswith("postgresql://"):
            pg_dsn = pg_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
        pool_min = settings.stores.postgres.pool_min
        pool_max = settings.stores.postgres.pool_max
        pg_engine = create_async_engine(
            pg_dsn, pool_size=pool_min, max_overflow=max(pool_max - pool_min, 0)
        )
        closers.append(("postgres", pg_engine.dispose))

        try:
            from redis.asyncio import Redis as AsyncRedis

            redis_client = AsyncRedis.from_url(
                settings.stores.redis.url,
                max_connections=settings.stores.redis.max_connections,
            )
            closers.append(("redis", redis_client.aclose))
            cache = RedisCache(redis_client)

            from arq.connections import RedisSettings, create_pool

            arq_pool = await create_pool(RedisSettings.from_dsn(settings.stores.redis.url))
            closers.append(("arq", arq_pool.aclose))

            from qdrant_client import AsyncQdrantClient

            qdrant_probe = AsyncQdrantClient(
                url=settings.stores.qdrant.url,
                prefer_grpc=False,  # REST is enough for a readyz ping; skip standing up gRPC
                timeout=settings.stores.qdrant.timeout_s,
            )
            closers.append(("qdrant_probe", qdrant_probe.close))

            qdrant_client = AsyncQdrantClient(
                url=settings.stores.qdrant.url,
                prefer_grpc=settings.stores.qdrant.prefer_grpc,
                timeout=settings.stores.qdrant.timeout_s,
            )
            closers.append(("qdrant", qdrant_client.close))

            from neo4j import AsyncGraphDatabase

            neo4j_driver = AsyncGraphDatabase.driver(
                settings.stores.neo4j.uri,
                auth=("neo4j", settings.secrets.neo4j_password.get_secret_value()),
            )
            closers.append(("neo4j_probe", neo4j_driver.close))

            # Sole credential this process holds for the gateway — see BLUEPRINT §5.6. Provider
            # keys live only in the LiteLLM proxy's own container environment, never here.
            # max_retries=0: LiteLLM (num_retries, fallbacks in litellm/config.yaml) owns provider
            # retries; the SDK's own retry layer must not also retry underneath LiteLLMClient's
            # own repair loop, or "max_repairs + 1 calls, never more" would not hold.
            openai_client = AsyncOpenAI(
                base_url=settings.llm.gateway_base_url,
                api_key=settings.secrets.litellm_virtual_key.get_secret_value(),
                timeout=settings.llm.request_timeout_s,
                max_retries=0,
            )
            closers.append(("litellm", openai_client.close))

            # Postgres and Redis are the two backends BO-03 depends on (ledger, sources, cache,
            # job queue); Qdrant joins them in BO-04 (vector_store, embedder) — verify all of
            # them eagerly so a broken container never reports itself as started. Neo4j/LiteLLM
            # are still probe-only; see the module docstring for why.
            async with pg_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await redis_client.ping()

            embedder = FastEmbedEmbedder(
                settings.embedding, cache if settings.cache.embedding.enabled else None
            )
            await _assert_embedding_dimensions(
                embedder, expected=settings.embedding.dense.dimensions
            )

            vector_store = QdrantVectorStore(qdrant_client, settings)
            await vector_store.ensure_collections()

            metrics = Metrics(meter())
            llm_client = LiteLLMClient(
                openai_client,
                llm=settings.llm,
                metrics=metrics,
                record_prompts=settings.observability.traces.record_prompts,
                max_recorded_prompt_chars=settings.observability.traces.max_recorded_prompt_chars,
                rate_limit_headers=tuple(settings.llm.adaptive_rate_limit.read_headers),
            )
        except Exception:
            for _name, closer in reversed(closers):
                try:
                    await closer()
                except Exception:
                    _log.warning("error closing %s during failed startup", _name, exc_info=True)
            raise

        ledger = PostgresDocumentLedger(pg_engine)
        sources = PostgresSourceRegistry(pg_engine)
        job_queue = ArqJobQueue(arq_pool)

        async def _probe_postgres() -> bool:
            async with pg_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True

        async def _probe_redis() -> bool:
            return bool(await redis_client.ping())

        async def _probe_qdrant() -> bool:
            await qdrant_probe.get_collections()
            return True

        async def _probe_neo4j() -> bool:
            await neo4j_driver.verify_connectivity()
            return True

        async def _probe_litellm() -> bool:
            return await llm_client.health()

        prober = ReadyzProber(
            {
                "postgres": _probe_postgres,
                "redis": _probe_redis,
                "qdrant": _probe_qdrant,
                "neo4j": _probe_neo4j,
                "litellm": _probe_litellm,
            },
            cache_s=settings.app.readyz_cache_s,
            timeout_s=settings.app.readyz_probe_timeout_ms / 1000,
        )

        return cls(
            settings=settings,
            ledger=ledger,
            sources=sources,
            cache=cache,
            job_queue=job_queue,
            readyz_prober=prober,
            metrics=metrics,
            vector_store=vector_store,
            embedder=embedder,
            llm_client=llm_client,
            closers=closers,
        )

    async def readyz(self) -> dict[str, bool]:
        return await self._readyz_prober.check()

    async def aclose(self) -> None:
        for name, closer in reversed(self._closers):
            try:
                await closer()
            except Exception:
                _log.warning("error closing %s", name, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    """Order: get_settings() -> init_telemetry -> configure_logging -> Container.create
    -> app.state.container -> yield -> Container.aclose -> shutdown_telemetry."""
    settings = get_settings()
    init_telemetry(settings, service_role="api")
    configure_logging(settings, service_role="api")
    container = await Container.create(settings)
    app.state.container = container
    try:
        yield
    finally:
        await container.aclose()
        shutdown_telemetry()


def create_app() -> FastAPI:
    """Middleware order, OUTERMOST first:
     1. CorrelationIdMiddleware
     2. AccessLogMiddleware
     3. CORS
     (4. RateLimitMiddleware — BO-12; not added yet)
    Then OTel FastAPI instrumentation, exception handlers, routers.
    All middleware are pure ASGI — no BaseHTTPMiddleware.

    Starlette's `add_middleware` makes the MOST RECENTLY added middleware the OUTERMOST one
    (it wraps everything added before it), so the calls below run in the reverse of the list
    above: CORS first, then AccessLog, then CorrelationId last.
    """
    settings = get_settings()
    app = FastAPI(title=settings.app.name, version=settings.app.version, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.app.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(CorrelationIdMiddleware, header_name=settings.security.correlation_id_header)

    FastAPIInstrumentor.instrument_app(app)
    install_exception_handlers(app)

    from graphrag.apps.api.routers import documents, health, jobs

    app.include_router(health.router)
    app.include_router(documents.router)
    app.include_router(jobs.router)

    return app
