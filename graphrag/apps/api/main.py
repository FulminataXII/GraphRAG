"""Container, lifespan, create_app. See BLUEPRINT §7.1.

BO-03 scope note: `Container` is the composition root for every port in `core.ports`, and its
shape (which fields exist) is meant to be stable from here on — but only `ledger`, `sources`,
`cache`, and `job_queue` have real adapters yet (Postgres/Redis/arq, all BO-03). `vector_store`,
`graph_store`, `embedder`, and `llm_client` stay `None` until BO-04/06/08 land their adapters;
nothing in this BO calls them.

`readyz` is the one place BO-03 needs to reach Qdrant, Neo4j, and the LiteLLM gateway (ARCHITECTURE
§2.1's health contract), before their owning BOs exist. Building the full `VectorStore`/
`GraphStore`/`LLMClient` adapters early to satisfy that would be building ahead of the current BO
(BO-04/06/08 own `ensure_collections`/`ensure_schema`/`structured()` and all the business logic
around them). Instead, `readyz`'s probes use the underlying client libraries directly — a raw
`get_collections()`/`verify_connectivity()`/HTTP ping — which only need "is this reachable",
not the ports' business methods. These probe-only clients are private to `Container` and are
never exposed as `vector_store`/`graph_store`/`llm_client`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from graphrag.adapters.arq_queue import ArqJobQueue
from graphrag.adapters.postgres.ledger import PostgresDocumentLedger
from graphrag.adapters.postgres.sources import PostgresSourceRegistry
from graphrag.adapters.redis_cache import RedisCache
from graphrag.adapters.telemetry.logging import configure_logging
from graphrag.adapters.telemetry.middleware import AccessLogMiddleware, CorrelationIdMiddleware
from graphrag.adapters.telemetry.otel import init_telemetry, shutdown_telemetry
from graphrag.apps.api.errors import install_exception_handlers
from graphrag.config.settings import Settings, get_settings

if TYPE_CHECKING:
    from graphrag.core.ports import Cache, DocumentLedger, Embedder, LLMClient
    from graphrag.core.ports import GraphStore as GraphStorePort
    from graphrag.core.ports import JobQueue as JobQueuePort
    from graphrag.core.ports import SourceRegistry as SourceRegistryPort
    from graphrag.core.ports import VectorStore as VectorStorePort

_log = logging.getLogger(__name__)

Probe = Callable[[], Awaitable[bool]]
Closer = Callable[[], Awaitable[None]]


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
        - create(settings) constructs clients and returns a ready container. Raises on any
          failure of a backend this BO actually depends on (Postgres, Redis) — the process must
          not start with a half-built container. Qdrant/Neo4j/LiteLLM reachability is NOT
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

            from neo4j import AsyncGraphDatabase

            neo4j_driver = AsyncGraphDatabase.driver(
                settings.stores.neo4j.uri,
                auth=("neo4j", settings.secrets.neo4j_password.get_secret_value()),
            )
            closers.append(("neo4j_probe", neo4j_driver.close))

            http_probe_client = httpx.AsyncClient(timeout=settings.stores.qdrant.timeout_s)
            closers.append(("http_probe", http_probe_client.aclose))

            # Postgres and Redis are the two backends this BO actually depends on (ledger,
            # sources, cache, job queue) — verify them eagerly so a broken container never
            # reports itself as started. Qdrant/Neo4j/LiteLLM are probe-only in BO-03; see the
            # module docstring for why they're not verified here.
            async with pg_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await redis_client.ping()
        except Exception:
            for _name, closer in reversed(closers):
                try:
                    await closer()
                except Exception:
                    _log.warning("error closing %s during failed startup", _name, exc_info=True)
            raise

        ledger = PostgresDocumentLedger(pg_engine)
        sources = PostgresSourceRegistry(pg_engine)
        cache = RedisCache(redis_client)
        job_queue = ArqJobQueue(arq_pool)

        litellm_base = settings.llm.gateway_base_url.rsplit("/v1", 1)[0]
        litellm_health_url = f"{litellm_base}/health/liveliness"

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
            response = await http_probe_client.get(litellm_health_url)
            response.raise_for_status()
            return True

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

    from graphrag.apps.api.routers import health

    app.include_router(health.router)

    return app
