"""Tracer/meter provider installation. See BLUEPRINT §4.1.

Owns the global TracerProvider and MeterProvider. Logs are a separate pipeline, deliberately
confined to `telemetry/logging.py` (see the warning there) — this module never imports
`opentelemetry.sdk._logs`, but it does delegate shutdown to `logging.shutdown_logging` so a
single `shutdown_telemetry()` call still flushes all three signals.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter
    from opentelemetry.trace import Tracer

    from graphrag.config.settings import Settings

_log = logging.getLogger(__name__)

_initialized = False
_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None


def _build_resource(settings: Settings, service_role: str) -> Resource:
    return Resource.create(
        {
            "service.name": settings.observability.service_name,
            "service.version": settings.app.version,
            "service.role": service_role,
            "deployment.environment": settings.app.env,
            "graphrag.config_hash": settings.config_hash,
        }
    )


def init_telemetry(settings: Settings, *, service_role: Literal["api", "worker", "cli"]) -> None:
    """Install global tracer and meter providers. Call ONCE, before anything else.

    Contract:
        - Resource attributes: service.name, service.version, service.role,
          deployment.environment, graphrag.config_hash.
        - Exporters: OTLP/gRPC to settings.observability.otlp_endpoint. Batch processors.
        - Sampler: ParentBased(TraceIdRatioBased(traces.sample_ratio)).
        - Idempotent: a second call is a no-op.
        - Never raises. If the collector is unreachable, telemetry degrades to no-op and a
          single warning is logged. Telemetry failure must NEVER fail a request.
    """
    global _initialized, _tracer_provider, _meter_provider
    if _initialized:
        return
    try:
        resource = _build_resource(settings, service_role)
        endpoint = settings.observability.otlp_endpoint

        tracer_provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(TraceIdRatioBased(settings.observability.traces.sample_ratio)),
        )
        if settings.observability.traces.enabled:
            tracer_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
            )
        otel_trace.set_tracer_provider(tracer_provider)
        _tracer_provider = tracer_provider

        readers = []
        if settings.observability.metrics.enabled:
            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=endpoint, insecure=True),
                    export_interval_millis=settings.observability.metrics.export_interval_ms,
                )
            )
        meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        otel_metrics.set_meter_provider(meter_provider)
        _meter_provider = meter_provider

        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument()
        except ImportError:
            pass

        try:
            from opentelemetry.instrumentation.redis import RedisInstrumentor

            RedisInstrumentor().instrument()
        except ImportError:
            pass

        try:
            from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor

            AsyncPGInstrumentor().instrument()
        except ImportError:
            pass

        _initialized = True
    except Exception:
        _log.warning("telemetry initialization failed; degrading to no-op", exc_info=True)


def shutdown_telemetry(timeout_s: float = 5.0) -> None:
    """Flush all pending spans/logs/metrics. Called from lifespan teardown."""
    global _initialized, _tracer_provider, _meter_provider
    timeout_ms = int(timeout_s * 1000)

    if _tracer_provider is not None:
        try:
            _tracer_provider.force_flush(timeout_ms)
            _tracer_provider.shutdown()
        except Exception:
            _log.warning("tracer provider shutdown failed", exc_info=True)
        _tracer_provider = None

    if _meter_provider is not None:
        try:
            _meter_provider.force_flush(timeout_ms)
            _meter_provider.shutdown()
        except Exception:
            _log.warning("meter provider shutdown failed", exc_info=True)
        _meter_provider = None

    try:
        from graphrag.adapters.telemetry.logging import shutdown_logging

        shutdown_logging(timeout_s=timeout_s)
    except Exception:
        _log.warning("logging shutdown failed", exc_info=True)

    _initialized = False


def tracer() -> Tracer:
    return otel_trace.get_tracer("graphrag")


def meter() -> Meter:
    return otel_metrics.get_meter("graphrag")
