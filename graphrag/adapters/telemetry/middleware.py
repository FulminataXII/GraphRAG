"""Pure ASGI middleware. See BLUEPRINT §4.4.

MUST NOT be `Starlette BaseHTTPMiddleware`: that implementation runs the endpoint in a separate
task, so contextvars bound inside the endpoint are invisible to middleware code that runs after
`await self.app(...)` returns — the access log would silently lose every field the endpoint
enriched. This is a genuine ASGI app/middleware (`__call__(scope, receive, send)`), which runs
the whole request in one coroutine, so contextvars propagate correctly end to end.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from opentelemetry import trace

from graphrag.adapters.telemetry.logging import bind_request_context, clear_request_context
from graphrag.core.ids import new_correlation_id

Scope = dict[str, Any]
Message = dict[str, Any]

_ACCESS_LOG_SKIP_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})


class CorrelationIdMiddleware:
    """Reads/generates the correlation ID and binds it for the lifetime of the request.

    Contract:
        - Reads security.correlation_id_header from the request; generates a ULID if absent.
        - Binds correlation_id into structlog contextvars AND sets it as span attribute
          'app.correlation_id' (this is what the trail CLI queries on).
        - Echoes the header on the response, including error responses.
        - Clears contextvars in a finally block.
    """

    def __init__(self, app: Any, *, header_name: str = "X-Correlation-ID") -> None:
        self._app = app
        self._header_bytes = header_name.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw = headers.get(self._header_bytes)
        correlation_id = raw.decode("latin-1") if raw else new_correlation_id()

        scope["correlation_id"] = correlation_id
        bind_request_context(correlation_id=correlation_id)
        trace.get_current_span().set_attribute("app.correlation_id", correlation_id)

        header_bytes = self._header_bytes
        value_bytes = correlation_id.encode("latin-1")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers_list = [*(message.get("headers") or []), (header_bytes, value_bytes)]
                message = {**message, "headers": headers_list}
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        finally:
            clear_request_context()


def _content_length(headers: list[tuple[bytes, bytes]]) -> int:
    for key, value in headers:
        if key == b"content-length":
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    if route is not None:
        path_format = getattr(route, "path_format", None) or getattr(route, "path", None)
        if path_format:
            return str(path_format)
    return str(scope.get("path", ""))


class AccessLogMiddleware:
    """Pure ASGI. Emits one 'http_request' event per request.

    Fields: method, path, route_template, status, duration_ms, request_bytes, response_bytes.
    Skips paths in {"/healthz", "/readyz", "/metrics"} to avoid log spam.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        if path in _ACCESS_LOG_SKIP_PATHS:
            await self._app(scope, receive, send)
            return

        start = time.perf_counter()
        request_bytes = _content_length(scope.get("headers") or [])
        response_state = {"status": 0, "response_bytes": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_state["status"] = message["status"]
            elif message["type"] == "http.response.body":
                response_state["response_bytes"] += len(message.get("body") or b"")
            await send(message)

        await self._app(scope, receive, send_wrapper)

        duration_ms = (time.perf_counter() - start) * 1000
        structlog.get_logger().info(
            "http_request",
            method=scope.get("method"),
            path=path,
            route_template=_route_template(scope),
            status=response_state["status"],
            duration_ms=round(duration_ms, 2),
            request_bytes=request_bytes,
            response_bytes=response_state["response_bytes"],
        )
