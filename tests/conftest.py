"""Shared test fixtures. See BLUEPRINT §9.

The session-scoped `stack` fixture (real docker-compose backends) lives in
`tests/integration/conftest.py`, scoped only to integration tests — importing it here would put
container-lifecycle machinery in the path of every unit test.
"""

from __future__ import annotations

import os

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from graphrag.apps.api.main import Container, ReadyzProber
from graphrag.config.settings import Settings
from tests.fakes import (
    FakeCache,
    FakeDocumentLedger,
    FakeEmbedder,
    FakeGraphStore,
    FakeJobQueue,
    FakeLLMClient,
    FakeSourceRegistry,
    FakeVectorStore,
)
from tests.unit._settings_helpers import REQUIRED_SECRET_ENV


@pytest.fixture(autouse=True)
def _isolated_cwd_and_env(
    request: pytest.FixtureRequest, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chdir to an empty tmp_path and clear every GRAPHRAG_* env var before each unit test.

    Settings() resolves its `.env` relative to the process cwd and reads GRAPHRAG_*-prefixed
    env vars directly — without this, a unit test could silently pass (or fail) depending on
    whatever real secrets/overrides happen to be sitting in the repo's `.env` or the
    developer's shell. A test that deliberately wants a `.env` writes one into tmp_path itself.

    Integration tests are exempt: they exercise the real stack via `docker compose`, which
    must run from the repo root (to find docker-compose.yml) against the real `.env` (which
    Compose itself reads for interpolation) — isolating them here would just break `make up`.
    """
    if "integration" in request.node.keywords:
        return
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("GRAPHRAG_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings loaded with APP_ENV=test (config/test.yaml) — the network-free, no-cache
    profile. Secrets are stubbed test values; nothing here holds a real credential."""
    monkeypatch.setenv("APP_ENV", "test")
    for key, value in REQUIRED_SECRET_ENV.items():
        monkeypatch.setenv(key, value)
    return Settings()


@pytest.fixture
def container(settings: Settings) -> Container:
    """All-fakes `Container` — every port backed by an in-memory `tests.fakes` implementation.

    Bypasses `Container.create()` (which builds real Postgres/Redis/arq/Qdrant/Neo4j clients)
    entirely: constructing `Container` directly with fakes is what keeps this a `unit` fixture.
    """
    empty_prober = ReadyzProber({}, cache_s=5, timeout_s=1)
    return Container(
        settings=settings,
        ledger=FakeDocumentLedger(),
        sources=FakeSourceRegistry(),
        cache=FakeCache(),
        job_queue=FakeJobQueue(),
        readyz_prober=empty_prober,
        vector_store=FakeVectorStore(),
        graph_store=FakeGraphStore(),
        embedder=FakeEmbedder(),
        llm_client=FakeLLMClient(),
    )


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    """An InMemorySpanExporter wired to the process-wide TracerProvider.

    `telemetry.decorators` always fetches the GLOBAL tracer (`otel.tracer()`), so tests that
    exercise `@traced` need spans to land somewhere inspectable on that same global provider.
    OTel's API refuses to replace an already-set global TracerProvider (a second
    `set_tracer_provider` call is a silent no-op with a warning), so this only installs one if
    none exists yet, then attaches a fresh exporter — safe to call from many tests in one
    process without clobbering state another test already set up.
    """
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter
