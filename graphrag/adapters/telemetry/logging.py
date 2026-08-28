"""structlog processor chain + OTLP logs bridge. See BLUEPRINT §4.2.

⚠️ The OTel Python *logs* pipeline is experimental — traces and metrics are not. The bridge
lives in `opentelemetry.sdk._logs` (leading underscore, no backward-compatibility guarantee).
ALL imports of that private API live in THIS module and nowhere else — enforced by
`test_otel_logs_imports_confined_to_one_module` (a static scan). If it breaks on an upstream
bump, the fallback is to drop `_install_otlp_log_handler` and keep the stdout renderer; traces,
metrics, and `trace_id`/`span_id` correlation on every JSON line are unaffected.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import TYPE_CHECKING, Any

import structlog
from opentelemetry import trace

if TYPE_CHECKING:
    from graphrag.config.settings import Settings

_REDACTED = "**********"
_MAX_REDACT_DEPTH = 6

_DEFAULT_REDACT_KEY_PATTERN = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)")
_SECRET_VALUE_PREFIXES: tuple[str, ...] = ("sk-", "gsk_", "AIza")

# Mutable module state, set by configure_logging() from observability.logs.redact_patterns.
# Adapters may hold module-level state (BLUEPRINT §0 restricts this to core/ and services/
# only); direct unit tests of redact_secrets() that never call configure_logging() still get
# a sensible default that mirrors config/base.yaml's own default pattern.
_redact_key_pattern: re.Pattern[str] = _DEFAULT_REDACT_KEY_PATTERN

_logger_provider: Any | None = None

# Config patterns (config/base.yaml's default included) embed an inline `(?i)` flag. Python
# 3.11+ rejects an inline flag that isn't at position 0 of the WHOLE compiled expression, so
# joining several such patterns with `|` would raise. Strip the inline flag from each pattern
# and apply re.IGNORECASE once at compile time instead.
_INLINE_IGNORECASE = re.compile(r"\(\?i\)")


def _configure_redaction(patterns: list[str]) -> None:
    global _redact_key_pattern
    if not patterns:
        _redact_key_pattern = _DEFAULT_REDACT_KEY_PATTERN
        return
    cleaned = [_INLINE_IGNORECASE.sub("", p) for p in patterns]
    _redact_key_pattern = re.compile("|".join(f"(?:{p})" for p in cleaned), re.IGNORECASE)


def add_otel_context(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Attach trace_id (32 hex) and span_id (16 hex) when a valid span is current.

    Contract: absent keys when no span is active — never emits nulls or zeros.
    """
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = format(span_context.trace_id, "032x")
        event_dict["span_id"] = format(span_context.span_id, "016x")
    return event_dict


def _looks_like_secret_value(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_SECRET_VALUE_PREFIXES)


def _redact(value: Any, depth: int) -> Any:
    if depth > _MAX_REDACT_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            key: _REDACTED if _redact_key_pattern.search(str(key)) else _redact(val, depth + 1)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, depth + 1) for item in value]
    if _looks_like_secret_value(value):
        return _REDACTED
    return value


def redact_secrets(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Replace values whose KEY matches observability.logs.redact_patterns with '**********'.

    Contract:
        - Recurses into nested dicts and lists.
        - Also redacts any string VALUE matching a known key prefix pattern (sk-, gsk_, AIza).
        - Depth-capped at 6 to bound cost.
    """
    return _redact(event_dict, 0)


def _add_static_fields(settings: Settings, service_role: str) -> Any:
    def processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        event_dict["service"] = settings.observability.service_name
        event_dict["role"] = service_role
        event_dict["env"] = settings.app.env
        event_dict["version"] = settings.app.version
        event_dict["config_hash"] = settings.config_hash
        return event_dict

    return processor


def _install_otlp_log_handler(
    root: logging.Logger, settings: Settings, service_role: str, shared_processors: list[Any]
) -> None:
    global _logger_provider
    try:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create(
            {
                "service.name": settings.observability.service_name,
                "service.version": settings.app.version,
                "service.role": service_role,
                "deployment.environment": settings.app.env,
                "graphrag.config_hash": settings.config_hash,
            }
        )
        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=settings.observability.otlp_endpoint, insecure=True)
            )
        )
        handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
        # Always JSON, independent of observability.logs.renderer — see configure_logging's
        # docstring for why the OTLP body can't follow the console-vs-json terminal choice.
        handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(),
                foreign_pre_chain=shared_processors,
            )
        )
        root.addHandler(handler)
        _logger_provider = provider
    except Exception:
        logging.getLogger(__name__).warning(
            "OTLP log handler init failed; continuing with stdout logging only", exc_info=True
        )


def configure_logging(settings: Settings, *, service_role: str) -> None:
    """Install the structlog processor chain and bridge stdlib logging to OTLP.

    Processor order (exact):
        1. structlog.contextvars.merge_contextvars
        2. add_log_level
        3. TimeStamper(fmt="iso", utc=True)
        4. add_otel_context          (trace_id, span_id — only when a span is recording)
        5. add_static_fields         (service, role, env, version, config_hash)
        6. redact_secrets
        7. StackInfoRenderer, format_exc_info
        8. ConsoleRenderer if observability.logs.renderer=="console" else JSONRenderer

    Step 8 is applied PER SINK, not once for the whole chain: steps 1-7 run exactly once per
    log call and produce one enriched event dict, but that dict is rendered separately for
    stdout (human's choice: console or JSON) and for OTLP export (always JSON). The OTLP body
    must stay JSON regardless of `logs.renderer` — ARCHITECTURE §3.4's Loki query is
    `{...} | json | correlation_id="<cid>"`, which requires a JSON body to parse; the whole
    point of `renderer: console` (config/local.yaml) is a human-readable *terminal*, and it
    would silently break trail/Loki querying if it also changed what got exported. This uses
    `structlog.stdlib.ProcessorFormatter` (steps 1-7 as `foreign_pre_chain` context, the
    renderer as the formatter's own `processor`) so each `logging.Handler` picks its own
    final rendering off one shared enriched event dict.
    """
    _configure_redaction(list(settings.observability.logs.redact_patterns))

    stdout_renderer: Any = (
        structlog.dev.ConsoleRenderer()
        if settings.observability.logs.renderer == "console"
        else structlog.processors.JSONRenderer()
    )

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_otel_context,
        _add_static_fields(settings, service_role),
        redact_secrets,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.setLevel(settings.observability.logs.level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=stdout_renderer, foreign_pre_chain=shared_processors
        )
    )
    root.addHandler(stream_handler)

    if settings.observability.logs.enabled:
        _install_otlp_log_handler(root, settings, service_role, shared_processors)


def shutdown_logging(timeout_s: float = 5.0) -> None:
    """Flush and shut down the OTLP logger provider, if one was installed."""
    global _logger_provider
    if _logger_provider is not None:
        try:
            _logger_provider.force_flush(int(timeout_s * 1000))
            _logger_provider.shutdown()
        except Exception:
            logging.getLogger(__name__).warning("logger provider shutdown failed", exc_info=True)
        _logger_provider = None


def bind_request_context(*, correlation_id: str, **extra: str) -> None:
    """structlog.contextvars.bind_contextvars wrapper. Task-local; safe under concurrency."""
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id, **extra)


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()
