"""BO-02 integration tests. See BUILD_ORDER.md.

Requires `make up obs=1` (core + obs profiles) already running. These exercise the real OTLP
pipeline: app process -> otel-collector -> otel-lgtm (Loki + Tempo), queried back out through
`TrailBuilder`. `config/local.yaml` points `observability.otlp_endpoint` and
`observability.trail.{loki,tempo}_url` at the published localhost ports (BUILD_ORDER BO-02) —
these tests run on the host like the CLI does, not inside the compose network.

`init_telemetry`/`shutdown_telemetry` are contracted as ONCE-per-process calls (BLUEPRINT
§4.1: "Call ONCE, before anything else" / "Called from lifespan teardown") — the OTel API
itself refuses to replace an already-registered global TracerProvider/MeterProvider (a second
`set_tracer_provider` is a silent no-op with a warning), so calling `init_telemetry` again
after `shutdown_telemetry` leaves `tracer()`/`meter()` pointed at the FIRST, now-shut-down
provider, and every span created afterward is silently dropped. So telemetry is initialized
ONCE for this whole module (session-scoped fixture) and each scenario only force-flushes —
never shuts down — between emitting and asserting.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator

import pytest
from opentelemetry.trace import Status, StatusCode
from typer.testing import CliRunner

from graphrag.adapters.telemetry import logging as telemetry_logging
from graphrag.adapters.telemetry import otel
from graphrag.adapters.telemetry.logging import (
    bind_request_context,
    clear_request_context,
    configure_logging,
)
from graphrag.adapters.telemetry.trail import TrailBuilder
from graphrag.apps.cli.main import app as cli_app
from graphrag.config.settings import Settings, get_settings
from graphrag.core.ids import new_correlation_id

pytestmark = pytest.mark.integration

# Tempo's search index lags ingestion by ~30-60s in this stack's default configuration (a
# span is fetchable by exact trace ID immediately, but doesn't show up in /api/search — which
# TrailBuilder must use, since it doesn't know the trace ID up front — until Tempo cuts and
# indexes the block it landed in). Measured empirically: found consistently within 60s, flaky
# right at 60s under load, so the timeout has real margin above the observed worst case.
_POLL_TIMEOUT_S = 150
_POLL_INTERVAL_S = 5


@pytest.fixture(scope="module")
def live_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(scope="module", autouse=True)
def _telemetry_session(live_settings: Settings) -> Iterator[None]:
    configure_logging(live_settings, service_role="api")
    otel.init_telemetry(live_settings, service_role="api")
    yield
    otel.shutdown_telemetry()


def _flush_all(timeout_s: float = 10.0) -> None:
    """Force-export whatever is queued, WITHOUT tearing the providers down (see module
    docstring for why a full shutdown_telemetry() between scenarios would be wrong here)."""
    if otel._tracer_provider is not None:
        otel._tracer_provider.force_flush(int(timeout_s * 1000))
    if otel._meter_provider is not None:
        otel._meter_provider.force_flush(int(timeout_s * 1000))
    if telemetry_logging._logger_provider is not None:
        telemetry_logging._logger_provider.force_flush(int(timeout_s * 1000))


def _emit_error_scenario(correlation_id: str) -> None:
    bind_request_context(correlation_id=correlation_id)
    try:
        with otel.tracer().start_as_current_span("integration.test_span") as span:
            span.set_attribute("app.correlation_id", correlation_id)
            try:
                raise RuntimeError("simulated failure for the trail roundtrip")
            except RuntimeError as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                import structlog

                structlog.get_logger().error("simulated_failure", detail=str(exc))
    finally:
        clear_request_context()

    _flush_all()


async def _build_until_populated(
    settings: Settings, correlation_id: str, *, expect_sources: set[str]
):
    """Poll until every source in `expect_sources` has at least one event.

    Loki is near-real-time; Tempo's /api/search index lags ingestion by ~30-60s (see the
    module comment on _POLL_TIMEOUT_S). A scenario emitting both a log and a span must wait
    for BOTH, not return the moment the (faster) log shows up.
    """
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    bundle = None
    while time.monotonic() < deadline:
        bundle = await TrailBuilder(settings).build(correlation_id)
        found = {e.source for e in bundle.events}
        if not bundle.sources_failed and expect_sources <= found:
            return bundle
        await asyncio.sleep(_POLL_INTERVAL_S)
    return bundle


def test_spans_reach_collector(live_settings: Settings) -> None:
    correlation_id = new_correlation_id()
    with otel.tracer().start_as_current_span("integration.spans_reach_collector") as span:
        span.set_attribute("app.correlation_id", correlation_id)
    _flush_all()

    bundle = asyncio.run(
        _build_until_populated(live_settings, correlation_id, expect_sources={"span"})
    )
    assert bundle is not None
    assert "tempo" not in bundle.sources_failed, bundle.sources_failed
    assert any(e.source == "span" for e in bundle.events), bundle.events


def test_trail_builder_merges_sources(live_settings: Settings) -> None:
    correlation_id = new_correlation_id()
    _emit_error_scenario(correlation_id)

    bundle = asyncio.run(
        _build_until_populated(live_settings, correlation_id, expect_sources={"log", "span"})
    )
    assert bundle is not None
    assert bundle.sources_failed == [], bundle.sources_failed
    sources = {e.source for e in bundle.events}
    assert sources == {"log", "span"}, bundle.events


def test_trail_cli_roundtrip(live_settings: Settings, tmp_path) -> None:
    correlation_id = new_correlation_id()
    _emit_error_scenario(correlation_id)

    # Poll TrailBuilder directly first so the test fails with a clear "propagation didn't
    # happen in time" message rather than a confusing empty CLI-written file.
    bundle = asyncio.run(
        _build_until_populated(live_settings, correlation_id, expect_sources={"log", "span"})
    )
    assert bundle is not None and bundle.events, "log/span never reached Loki/Tempo in time"

    out_path = tmp_path / f"debug_bundle_{correlation_id}.md"
    runner = CliRunner()
    result = runner.invoke(cli_app, ["trail", correlation_id, "--out", str(out_path)])

    assert result.exit_code == 0, result.output
    assert out_path.is_file()
    content = out_path.read_text(encoding="utf-8")

    assert correlation_id in content
    assert "simulated failure for the trail roundtrip" in content
    assert "integration.test_span" in content
    assert content.count("|") > 4  # at least one row in the timeline table
