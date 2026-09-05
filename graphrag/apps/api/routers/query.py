from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from graphrag.apps.api.deps import get_container
from graphrag.apps.api.errors import ErrorEnvelope
from graphrag.apps.api.main import Container
from graphrag.core.models import Answer, Citation, RoutePlan, Spend

router = APIRouter(prefix="/v1/query", tags=["query"])

ERROR_RESPONSES = {
    500: {"model": ErrorEnvelope, "description": "Internal Server Error"},
    502: {"model": ErrorEnvelope, "description": "Bad Gateway (e.g. LLMSchemaViolation)"},
    503: {"model": ErrorEnvelope, "description": "Service Unavailable (e.g. LLMProviderExhausted)"},
}


class QueryRequest(BaseModel):
    question: str
    top_k: int | None = None
    strategy: Literal["vector", "graph", "hybrid"] | None = None


class QueryResponse(BaseModel):
    answer: Answer
    citations: list[Citation]
    route: RoutePlan | None
    degraded: list[str]
    spent: Spend
    correlation_id: str


@router.post("", responses=ERROR_RESPONSES)
async def query_sync(
    request: QueryRequest,
    http_request: Request,
    container: Container = Depends(get_container),
) -> QueryResponse:
    # The correlation id is bound ONCE per request, by CorrelationIdMiddleware, which also
    # echoes it on the response header and sets it as the `app.correlation_id` span attribute
    # the trail query keys on. Minting a second one here would hand the client an id that
    # matches neither the header, the logs, nor the trace. Same read as `errors.py`.
    cid = str(http_request.scope.get("correlation_id") or "unknown")
    result = await container.orchestrator.run(
        request.question, cid, strategy=request.strategy, top_k=request.top_k
    )

    # Extract citations from answer if present
    citations = result.answer.citations if result.answer else []

    return QueryResponse(
        answer=result.answer,
        citations=citations,
        route=result.route,
        degraded=result.degraded,
        spent=result.spent,
        correlation_id=result.correlation_id,
    )


@router.post("/stream")
async def query_stream(
    request: QueryRequest,
    http_request: Request,
    container: Container = Depends(get_container),
) -> StreamingResponse:
    cid = str(http_request.scope.get("correlation_id") or "unknown")

    async def sse_generator():
        async for event in container.orchestrator.stream(
            request.question, cid, strategy=request.strategy, top_k=request.top_k
        ):
            yield f"data: {event.model_dump_json()}\n\n"

    return StreamingResponse(sse_generator(), media_type="text/event-stream")
