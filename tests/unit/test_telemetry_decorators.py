"""BO-02: telemetry/decorators.py. See BUILD_ORDER.md."""

from __future__ import annotations

import pytest

from graphrag.adapters.telemetry.decorators import traced


def test_traced_records_only_listed_args(span_exporter) -> None:
    @traced(record_args=["keep"])
    def fn(keep: str, drop: str) -> str:
        return keep + drop

    result = fn("visible", "hidden-secret")
    assert result == "visiblehidden-secret"

    (span,) = span_exporter.get_finished_spans()
    assert span.attributes["keep"] == "visible"
    assert "drop" not in span.attributes


def test_traced_default_span_name(span_exporter) -> None:
    @traced()
    def some_function() -> None:
        return None

    some_function()
    (span,) = span_exporter.get_finished_spans()
    assert span.name == f"test_telemetry_decorators.{some_function.__wrapped__.__qualname__}"


def test_traced_custom_span_name(span_exporter) -> None:
    @traced(name="custom.name")
    def fn() -> None:
        return None

    fn()
    (span,) = span_exporter.get_finished_spans()
    assert span.name == "custom.name"


def test_traced_records_exception_and_reraises(span_exporter) -> None:
    class BoomError(RuntimeError):
        pass

    @traced()
    def fn() -> None:
        raise BoomError("kaboom")

    with pytest.raises(BoomError):
        fn()

    (span,) = span_exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert any(event.name == "exception" for event in span.events)


async def _async_fn(keep: str) -> str:
    return keep


def test_traced_works_on_sync_and_async(span_exporter) -> None:
    @traced(record_args=["keep"])
    def sync_fn(keep: str) -> str:
        return keep

    traced_async = traced(record_args=["keep"])(_async_fn)

    assert sync_fn("a") == "a"

    import asyncio

    assert asyncio.run(traced_async("b")) == "b"

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 2
    assert {s.attributes["keep"] for s in spans} == {"a", "b"}


def test_traced_preserves_wrapped_metadata() -> None:
    @traced()
    def documented(x: int) -> int:
        """Docstring."""
        return x

    assert documented.__name__ == "documented"
    assert documented.__wrapped__ is not None


def test_traced_async_detection_uses_iscoroutinefunction(span_exporter) -> None:
    import asyncio
    import inspect

    @traced()
    async def async_fn() -> int:
        return 42

    assert inspect.iscoroutinefunction(async_fn.__wrapped__)
    assert asyncio.run(async_fn()) == 42
