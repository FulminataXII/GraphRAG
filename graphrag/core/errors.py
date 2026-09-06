"""AppError hierarchy. See BLUEPRINT §3.3.

Every error crossing a service boundary is one of these subclasses (or a library exception
that an adapter has not yet wrapped — adapters wrap, services never raise library exceptions).
"""

from __future__ import annotations

from typing import Any, ClassVar


class AppError(Exception):
    """Base. Every error crossing a service boundary is one of these.

    Contract:
        - `code` is stable and machine-readable; it appears verbatim in API responses.
        - `http_status` and `retryable` are class attributes, not instance state.
        - `details` must never contain secrets or raw prompts.
    """

    code: ClassVar[str]
    http_status: ClassVar[int]
    retryable: ClassVar[bool]

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(AppError):
    code: ClassVar[str] = "VALIDATION_ERROR"
    http_status: ClassVar[int] = 422
    retryable: ClassVar[bool] = False


class AuthInvalidKey(AppError):
    code: ClassVar[str] = "AUTH_INVALID_KEY"
    http_status: ClassVar[int] = 401
    retryable: ClassVar[bool] = False


class RateLimited(AppError):
    code: ClassVar[str] = "RATE_LIMITED"
    http_status: ClassVar[int] = 429
    retryable: ClassVar[bool] = True

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.retry_after = retry_after


class LLMSchemaViolation(AppError):
    code: ClassVar[str] = "LLM_SCHEMA_VIOLATION"
    http_status: ClassVar[int] = 502
    retryable: ClassVar[bool] = True


class LLMProviderExhausted(AppError):
    code: ClassVar[str] = "LLM_PROVIDER_EXHAUSTED"
    http_status: ClassVar[int] = 503
    retryable: ClassVar[bool] = True


class RetrievalBackendUnavailable(AppError):
    code: ClassVar[str] = "RETRIEVAL_BACKEND_UNAVAILABLE"
    http_status: ClassVar[int] = 503
    retryable: ClassVar[bool] = True


class GraphBackendUnavailable(AppError):
    code: ClassVar[str] = "GRAPH_BACKEND_UNAVAILABLE"
    http_status: ClassVar[int] = 503
    retryable: ClassVar[bool] = True


class BudgetExceeded(AppError):
    code: ClassVar[str] = "BUDGET_EXCEEDED"
    http_status: ClassVar[int] = 200
    retryable: ClassVar[bool] = False


class NotFound(AppError):
    code: ClassVar[str] = "NOT_FOUND"
    http_status: ClassVar[int] = 404
    retryable: ClassVar[bool] = False


class ConflictError(AppError):
    code: ClassVar[str] = "CONFLICT"
    http_status: ClassVar[int] = 409
    retryable: ClassVar[bool] = False


class JobTimeout(AppError):
    """A background job was cancelled by arq's `job_timeout` before it finished.

    Never raised by a service — `apps/worker/tasks/_common.py` constructs it when it catches
    `asyncio.CancelledError`, purely so the ledger records a status distinguishable from an
    ordinary crash. Without it, a timed-out job leaves its document stranded at whatever
    in-progress status it held (EXTRACTING/RESOLVING) with nothing in the system able to detect
    or re-drive it: `CancelledError` inherits `BaseException`, so the task wrapper's
    `except Exception` never saw it and no failure handler ran.

    `retryable` is True because the work genuinely may succeed on a re-run (`graphrag ingest
    --force <path>`), not because anything retries it automatically — nothing does.
    """

    code: ClassVar[str] = "JOB_TIMEOUT"
    http_status: ClassVar[int] = 504
    retryable: ClassVar[bool] = True


class InternalError(AppError):
    code: ClassVar[str] = "INTERNAL_ERROR"
    http_status: ClassVar[int] = 500
    retryable: ClassVar[bool] = False
