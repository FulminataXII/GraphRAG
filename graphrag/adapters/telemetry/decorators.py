"""Span/metric decorators for the service boundary. See BLUEPRINT §4.3.

Four instrumentation mechanisms exist (ARCHITECTURE §3.3); these decorators are #3. Business
logic stays untouched — the decorator opens the span/records the metric and gets out of the way.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from opentelemetry.trace import Status, StatusCode

from graphrag.adapters.telemetry.otel import meter, tracer

F = TypeVar("F", bound=Callable[..., Any])

_MAX_ARG_CHARS = 200

_counters: dict[str, Any] = {}
_histograms: dict[str, Any] = {}


def _default_span_name(func: Callable[..., Any]) -> str:
    module_basename = func.__module__.rsplit(".", 1)[-1]
    return f"{module_basename}.{func.__qualname__}"


def _bind_named_args(
    func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any], names: Sequence[str]
) -> dict[str, str]:
    """Resolve only the listed parameter names to str values, truncated to 200 chars.

    Never records **kwargs wholesale — only names explicitly listed by the caller.
    """
    if not names:
        return {}
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
        bound.apply_defaults()
    except TypeError:
        return {}
    return {
        name: str(bound.arguments[name])[:_MAX_ARG_CHARS]
        for name in names
        if name in bound.arguments
    }


def _get_counter(metric: str) -> Any:
    if metric not in _counters:
        _counters[metric] = meter().create_counter(metric)
    return _counters[metric]


def _get_histogram(metric: str) -> Any:
    if metric not in _histograms:
        _histograms[metric] = meter().create_histogram(metric, unit="ms")
    return _histograms[metric]


def traced(
    name: str | None = None,
    *,
    record_args: Sequence[str] = (),
    record_result_len: bool = False,
) -> Callable[[F], F]:
    """Wrap an async or sync callable in a span.

    Contract:
        - name defaults to f"{module_basename}.{qualname}".
        - Records ONLY the parameters listed in record_args, by name, coerced to str and
          truncated to 200 chars. Never records **kwargs wholesale.
        - On exception: span.record_exception, status=ERROR, then re-raise unchanged.
        - Preserves signature and __wrapped__ (functools.wraps).
        - Zero-config: works on both sync and async functions; detects via iscoroutinefunction.
    """

    def decorator(func: F) -> F:
        span_name = name or _default_span_name(func)
        is_async = inspect.iscoroutinefunction(func)

        if is_async:

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                attrs = _bind_named_args(func, args, kwargs, record_args)
                with tracer().start_as_current_span(span_name, attributes=attrs) as span:
                    try:
                        result = await func(*args, **kwargs)
                    except Exception as exc:
                        span.record_exception(exc)
                        span.set_status(Status(StatusCode.ERROR, str(exc)))
                        raise
                    _maybe_record_result_len(span, result, record_result_len)
                    _maybe_record_node_failures(span, result)
                    return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            attrs = _bind_named_args(func, args, kwargs, record_args)
            with tracer().start_as_current_span(span_name, attributes=attrs) as span:
                try:
                    result = func(*args, **kwargs)
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    raise
                _maybe_record_result_len(span, result, record_result_len)
                _maybe_record_node_failures(span, result)
                return result

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def _maybe_record_result_len(span: Any, result: Any, enabled: bool) -> None:
    if not enabled:
        return
    with contextlib.suppress(TypeError):
        span.set_attribute("result_len", len(result))


def _maybe_record_node_failures(span: Any, result: Any) -> None:
    if isinstance(result, dict) and result.get("failures"):
        span.set_attribute("node.has_failures", True)
        span.add_event("node_failure_recorded")


def counted(metric: str, *, labels: Sequence[str] = ()) -> Callable[[F], F]:
    """Increment a counter on each call with outcome=success|error plus listed label values."""

    def decorator(func: F) -> F:
        is_async = inspect.iscoroutinefunction(func)

        if is_async:

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                attrs = _bind_named_args(func, args, kwargs, labels)
                try:
                    result = await func(*args, **kwargs)
                except Exception:
                    _record_outcome(metric, attrs, "error")
                    raise
                _record_outcome(metric, attrs, "success")
                return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            attrs = _bind_named_args(func, args, kwargs, labels)
            try:
                result = func(*args, **kwargs)
            except Exception:
                _record_outcome(metric, attrs, "error")
                raise
            _record_outcome(metric, attrs, "success")
            return result

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def _record_outcome(metric: str, attrs: Mapping[str, str], outcome: str) -> None:
    _get_counter(metric).add(1, {**attrs, "outcome": outcome})


def timed(metric: str, *, labels: Sequence[str] = ()) -> Callable[[F], F]:
    """Record duration in a histogram (milliseconds)."""

    def decorator(func: F) -> F:
        is_async = inspect.iscoroutinefunction(func)

        if is_async:

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                attrs = _bind_named_args(func, args, kwargs, labels)
                start = time.perf_counter()
                try:
                    return await func(*args, **kwargs)
                finally:
                    _get_histogram(metric).record((time.perf_counter() - start) * 1000, attrs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            attrs = _bind_named_args(func, args, kwargs, labels)
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                _get_histogram(metric).record((time.perf_counter() - start) * 1000, attrs)

        return sync_wrapper  # type: ignore[return-value]

    return decorator
