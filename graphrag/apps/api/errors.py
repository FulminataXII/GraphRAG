"""Error envelope + exception handlers. See BLUEPRINT §7.1.

Every response — success or error — carries `correlation_id` (bound by
`telemetry.middleware.CorrelationIdMiddleware`) and, when a span is active, `trace_id`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from pydantic import BaseModel

from graphrag.core.errors import AppError, RateLimited

_log = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    code: str
    message: str
    correlation_id: str
    trace_id: str | None
    retryable: bool
    details: dict[str, Any] = {}


class ErrorEnvelope(BaseModel):
    error: ErrorBody


def _correlation_id(request: Request) -> str:
    return str(request.scope.get("correlation_id") or "unknown")


def _trace_id() -> str | None:
    span_context = trace.get_current_span().get_span_context()
    return format(span_context.trace_id, "032x") if span_context.is_valid else None


def _envelope_for(request: Request, exc: AppError) -> ErrorEnvelope:
    details = dict(exc.details)
    if isinstance(exc, RateLimited) and exc.retry_after is not None:
        details.setdefault("retry_after", exc.retry_after)
    return ErrorEnvelope(
        error=ErrorBody(
            code=exc.code,
            message=exc.message,
            correlation_id=_correlation_id(request),
            trace_id=_trace_id(),
            retryable=exc.retryable,
            details=details,
        )
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Maps AppError -> ErrorEnvelope with its code/status/retryable.

    Unhandled Exception -> INTERNAL_ERROR 500: logs the full traceback, returns NO traceback.
    """

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        envelope = _envelope_for(request, exc)
        return JSONResponse(status_code=exc.http_status, content=envelope.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def _handle_unhandled(request: Request, exc: Exception) -> JSONResponse:
        _log.error("unhandled exception", exc_info=exc)
        envelope = ErrorEnvelope(
            error=ErrorBody(
                code="INTERNAL_ERROR",
                message="internal server error",
                correlation_id=_correlation_id(request),
                trace_id=_trace_id(),
                retryable=False,
                details={},
            )
        )
        return JSONResponse(status_code=500, content=envelope.model_dump(mode="json"))
