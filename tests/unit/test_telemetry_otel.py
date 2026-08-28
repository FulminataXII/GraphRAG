"""BO-02: telemetry/otel.py. See BUILD_ORDER.md."""

from __future__ import annotations

import pytest

from graphrag.adapters.telemetry import otel
from graphrag.config.settings import Settings
from tests.unit._settings_helpers import set_required_secrets


@pytest.fixture
def traces_and_metrics_enabled_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """config/test.yaml turns traces/metrics export off (BLUEPRINT: network-free unit tests),
    so `init_telemetry` would never even reach the exporter constructors under the shared
    `settings` fixture. Force them on here so the exporter-construction failure path this test
    exists to cover is actually exercised."""
    monkeypatch.setenv("APP_ENV", "test")
    set_required_secrets(monkeypatch)
    monkeypatch.setenv("GRAPHRAG_OBSERVABILITY__TRACES__ENABLED", "true")
    monkeypatch.setenv("GRAPHRAG_OBSERVABILITY__METRICS__ENABLED", "true")
    return Settings()


def test_telemetry_init_never_raises(
    traces_and_metrics_enabled_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated exporter construction failure")

    monkeypatch.setattr(otel, "OTLPSpanExporter", _boom)
    monkeypatch.setattr(otel, "OTLPMetricExporter", _boom)

    otel._initialized = False
    otel._tracer_provider = None
    otel._meter_provider = None
    try:
        otel.init_telemetry(traces_and_metrics_enabled_settings, service_role="api")  # no raise
        assert otel._initialized is False  # the whole init failed and rolled back to no-op
    finally:
        otel._initialized = False
        otel._tracer_provider = None
        otel._meter_provider = None


def test_telemetry_init_is_idempotent(settings: Settings) -> None:
    otel._initialized = False
    otel._tracer_provider = None
    otel._meter_provider = None
    try:
        otel.init_telemetry(settings, service_role="api")
        first_provider = otel._tracer_provider
        otel.init_telemetry(settings, service_role="worker")
        assert otel._tracer_provider is first_provider
    finally:
        otel.shutdown_telemetry()


def test_tracer_and_meter_return_real_instruments() -> None:
    tracer = otel.tracer()
    meter = otel.meter()
    with tracer.start_as_current_span("smoke"):
        pass
    counter = meter.create_counter("smoke_counter")
    counter.add(1)
