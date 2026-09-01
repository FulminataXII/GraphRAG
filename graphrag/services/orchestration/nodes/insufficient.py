from __future__ import annotations

from typing import TYPE_CHECKING, Any

from graphrag.core.models import Answer, Citation

if TYPE_CHECKING:
    from graphrag.services.orchestration.graph import NodeDeps
from graphrag.services.orchestration.state import QueryState


async def node(state: QueryState, deps: NodeDeps) -> dict[str, Any]:
    fused = state.get("fused", [])
    graded = state.get("graded", [])
    failures = state.get("failures", [])

    reason = "budget"
    if not fused:
        reason = "no_retrieval"
    elif not graded:
        reason = "irrelevant_context"
    elif failures:
        reason = "error"

    deps.metrics.answer_refused.add(1, {"reason": reason})

    text = "I do not have enough information to answer that question."

    citations = []
    chunks_to_cite = graded if graded else fused
    for c in chunks_to_cite:
        if c.chunk.sources:
            source = c.chunk.sources[0]
            citations.append(
                Citation(
                    chunk_id=c.chunk.chunk_id,
                    doc_id=source.doc_id,
                    uri=source.uri,
                    quote=None,
                )
            )

    return {
        "answer": Answer(
            text=text,
            citations=citations,
            confidence=1.0,
        )
    }
