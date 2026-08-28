"""BO-02: telemetry/trail.py. See BUILD_ORDER.md.

Uses `httpx.MockTransport` to fake the Loki/Tempo HTTP APIs — no real backends, no extra test
dependency (MockTransport ships in httpx, already a project dependency).
"""

from __future__ import annotations

import json

import httpx

from graphrag.adapters.telemetry.trail import TrailBuilder
from graphrag.config.settings import Settings


def _loki_body(correlation_id: str, *, secret: bool = True) -> dict:
    fields = {
        "event": "did_a_thing",
        "level": "info",
        "correlation_id": correlation_id,
        "trace_id": "a" * 32,
    }
    if secret:
        fields["api_key"] = "sk-should-be-redacted"
    line = json.dumps(fields)
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {"stream": {"service": "graphrag"}, "values": [["1700000000000000000", line]]}
            ],
        },
    }


async def test_trail_survives_backend_outage(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/loki/" in request.url.path:
            return httpx.Response(200, json=_loki_body("cid-1"))
        raise httpx.ConnectError("tempo is down", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    builder = TrailBuilder(settings, http_client=client)
    try:
        bundle = await builder.build("cid-1")
    finally:
        await client.aclose()

    assert bundle.sources_failed == ["tempo"]
    assert bundle.trace_ids == []
    assert len(bundle.events) == 1
    assert bundle.events[0].source == "log"
    assert bundle.events[0].message == "did_a_thing"
    # redact_secrets was applied before rendering
    assert "sk-should-be-redacted" not in bundle.to_markdown()


async def test_trail_survives_both_backends_down(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing is up", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    builder = TrailBuilder(settings, http_client=client)
    try:
        bundle = await builder.build("cid-down")  # must not raise
    finally:
        await client.aclose()

    assert set(bundle.sources_failed) == {"loki", "tempo"}
    assert bundle.events == []
    assert bundle.correlation_id == "cid-down"
    # Still renders a valid, non-empty markdown bundle.
    markdown = bundle.to_markdown()
    assert "cid-down" in markdown
    assert "loki" in markdown and "tempo" in markdown


async def test_trail_builder_caps_at_max_events(settings: Settings) -> None:
    trail_cfg = settings.observability.trail
    max_events = trail_cfg.max_events

    def handler(request: httpx.Request) -> httpx.Response:
        if "/loki/" in request.url.path:
            lines = [
                [
                    str(1_700_000_000_000_000_000 + i),
                    json.dumps({"event": f"event-{i}", "level": "info"}),
                ]
                for i in range(max_events + 5)
            ]
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [{"stream": {}, "values": lines}],
                    },
                },
            )
        return httpx.Response(200, json={"traces": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    builder = TrailBuilder(settings, http_client=client)
    try:
        bundle = await builder.build("cid-many")
    finally:
        await client.aclose()

    assert bundle.truncated is True
    assert len(bundle.events) == max_events


async def test_trail_builder_merges_and_orders_logs_and_spans(settings: Settings) -> None:
    trace_id = "b" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        if "/loki/" in request.url.path:
            return httpx.Response(200, json=_loki_body("cid-merge", secret=False))
        if request.url.path == "/api/search":
            return httpx.Response(200, json={"traces": [{"traceID": trace_id}]})
        if request.url.path == f"/api/traces/{trace_id}":
            return httpx.Response(
                200,
                json={
                    "resourceSpans": [
                        {
                            "scopeSpans": [
                                {
                                    "spans": [
                                        {
                                            "name": "root-span",
                                            "spanId": "c" * 16,
                                            "startTimeUnixNano": "1699999999000000000",
                                            "attributes": [
                                                {
                                                    "key": "app.correlation_id",
                                                    "value": {"stringValue": "cid-merge"},
                                                }
                                            ],
                                            "status": {"code": 0},
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    builder = TrailBuilder(settings, http_client=client)
    try:
        bundle = await builder.build("cid-merge")
    finally:
        await client.aclose()

    assert bundle.sources_failed == []
    assert bundle.trace_ids == [trace_id]
    assert [e.source for e in bundle.events] == ["span", "log"]  # span timestamp is earlier
    assert bundle.events[0].attributes["app.correlation_id"] == "cid-merge"

    markdown = bundle.to_markdown()
    assert "root-span" in markdown
    assert "did_a_thing" in markdown
