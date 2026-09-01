from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from graphrag.apps.api.deps import get_container
from graphrag.apps.api.errors import ErrorEnvelope
from graphrag.apps.api.main import Container
from graphrag.core.ids import new_correlation_id
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
    container: Container = Depends(get_container),
) -> QueryResponse:
    # Normally we'd get correlation_id from middleware, for now generate one
    cid = new_correlation_id()
    result = await container.orchestrator.run(request.question, cid)

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
    container: Container = Depends(get_container),
) -> StreamingResponse:
    cid = new_correlation_id()

    async def sse_generator():
        async for event in container.orchestrator.stream(request.question, cid):
            yield f"data: {event.model_dump_json()}\n\n"

    return StreamingResponse(sse_generator(), media_type="text/event-stream")
