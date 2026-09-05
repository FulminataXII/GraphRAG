"""The `correlation_id` a handler returns must be the one the middleware bound. See BLUEPRINT §7.1.

`CorrelationIdMiddleware` (BLUEPRINT §4.4) reads or generates the id ONCE per request, writes it
to `scope["correlation_id"]`, binds it into structlog, sets it as the `app.correlation_id` span
attribute, and echoes it on the response header. Everything downstream — the access log, the
trace, `graphrag trail <cid>` (BLUEPRINT §4.6), and `GET /v1/debug/trail/{cid}` — keys on that one
value.

A handler that mints its own id instead returns something that correlates with nothing: the body
and the header disagree, and the id a user quotes from a response resolves to an empty trail
bundle, which is indistinguishable from "OTLP logs were never exported". That equality is the
whole property, so it is asserted here directly, against the REAL `create_app()` (not a bare
`FastAPI()` with routers attached) — the middleware is part of what is under test.

`/v1/query/stream` is deliberately absent: its response body is an SSE event stream and
`StreamEvent` carries no correlation_id field, so there is no body value to compare the header
against. The id still reaches the orchestrator by the same `scope` read.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from graphrag.apps.api.deps import get_container
from graphrag.apps.api.main import Container, create_app
from graphrag.services.orchestration.schemas import RewrittenQuery, RoutePlanOut

_HEADER = "X-Correlation-ID"


@pytest.fixture
async def client(container: Container) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


def _script_refusal(container: Any) -> None:
    """Drive `/v1/query` to its refusal path with the fewest scripted calls.

    `FakeVectorStore` is empty, so `fused` is empty, so `grade_context` short-circuits without
    an LLM call and `graded` stays empty. `route_after_grade` then rewrites until
    `max_query_rewrites` (2) and refuses. `rewrite_query` loops back to `plan_route`, which
    re-plans, so that is three `RoutePlanOut` and two `RewrittenQuery`. A refusal is a 200
    (BLUEPRINT §6.4), which is all this test needs.
    """
    plan_out = RoutePlanOut(
        strategy="vector",
        template="neighbors",
        seed_entities=[],
        hops=1,
        sub_queries=[],
        rationale="",
    )
    container.llm_client.script_structured(
        "router",
        plan_out,
        plan_out,
        plan_out,
        RewrittenQuery(query="rewrite-1", changed_because=""),
        RewrittenQuery(query="rewrite-2", changed_because=""),
    )


async def test_query_echoes_the_client_supplied_correlation_id(
    container: Any, client: httpx.AsyncClient
) -> None:
    """A client that supplies the header gets that exact id back in the body."""
    _script_refusal(container)
    supplied = "01JCLIENTSUPPLIEDCID000000"

    response = await client.post(
        "/v1/query", json={"question": "test"}, headers={_HEADER: supplied}
    )

    assert response.status_code == 200
    assert response.headers[_HEADER] == supplied
    assert response.json()["correlation_id"] == supplied


async def test_query_body_correlation_id_equals_response_header(
    container: Any, client: httpx.AsyncClient
) -> None:
    """With no header supplied, the middleware generates one — the body must report THAT one."""
    _script_refusal(container)

    response = await client.post("/v1/query", json={"question": "test"})

    assert response.status_code == 200
    generated = response.headers[_HEADER]
    assert generated, "CorrelationIdMiddleware must echo a generated id on the response"
    assert response.json()["correlation_id"] == generated


async def test_document_upload_body_correlation_id_equals_response_header(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/documents",
        files={"file": ("a.txt", b"hello world " * 20, "text/plain")},
        headers={"Idempotency-Key": "key-1"},
    )

    assert response.status_code == 202
    assert response.json()["correlation_id"] == response.headers[_HEADER]


async def test_document_delete_body_correlation_id_equals_response_header(
    client: httpx.AsyncClient,
) -> None:
    supplied = "01JDELETESUPPLIEDCID000000"

    response = await client.delete("/v1/documents/doc-1", headers={_HEADER: supplied})

    assert response.status_code == 202
    assert response.headers[_HEADER] == supplied
    assert response.json()["correlation_id"] == supplied


async def test_error_response_reports_the_same_correlation_id_as_a_success(
    client: httpx.AsyncClient,
) -> None:
    """`errors.py` already read the scope correctly, so before the fix a success body and an
    error body reported DIFFERENT ids for the same request. Pin that they agree."""
    supplied = "01JERRORPATHSAMECID0000000"

    # Missing Idempotency-Key -> ValidationError -> ErrorEnvelope from `install_exception_handlers`.
    error = await client.post(
        "/v1/documents",
        files={"file": ("a.txt", b"hello world " * 20, "text/plain")},
        headers={_HEADER: supplied},
    )
    assert error.status_code == 422
    assert error.json()["error"]["correlation_id"] == supplied

    ok = await client.post(
        "/v1/documents",
        files={"file": ("a.txt", b"hello world " * 20, "text/plain")},
        headers={_HEADER: supplied, "Idempotency-Key": "key-1"},
    )
    assert ok.status_code == 202
    assert ok.json()["correlation_id"] == error.json()["error"]["correlation_id"]
