from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from graphrag.core.models import Answer, Citation, Spend

if TYPE_CHECKING:
    from graphrag.services.orchestration.graph import NodeDeps
from graphrag.services.orchestration.prompts import render
from graphrag.services.orchestration.schemas import AnswerOut
from graphrag.services.orchestration.state import QueryState


async def node(state: QueryState, deps: NodeDeps) -> dict[str, Any]:
    question = state["question"]
    graded = state.get("graded", [])

    chunks = [c.chunk for c in graded]
    prompt = render("generate.j2", question=question, chunks=chunks)

    result = await deps.llm.structured(
        role="synth",
        messages=[{"role": "user", "content": prompt}],
        schema=AnswerOut,
        max_repairs=deps.settings.orchestration.max_structured_output_repairs,
    )
    spent = Spend(
        llm_calls=result.repair_attempts + 1,
        tokens=result.prompt_tokens + result.completion_tokens,
        wall_ms=result.latency_ms,
    )

    # Convert AnswerOut to Answer
    answer_out = result.value

    # We map chunk_id -> chunk to get doc_id and uri
    chunk_map = {str(c.chunk.chunk_id): c.chunk for c in graded}

    citations = []
    for cit_out in answer_out.citations:
        chunk = chunk_map.get(cit_out.chunk_id)
        if chunk and chunk.sources:
            # We just use the first source for citation as per typical RAG
            # Or if no sources, we provide some fallback. BLUEPRINT says sources are hydrated.
            source = chunk.sources[0]
            try:
                cid = uuid.UUID(cit_out.chunk_id)
            except ValueError:
                cid = uuid.UUID(int=0)

            citations.append(
                Citation(
                    chunk_id=cid,
                    doc_id=source.doc_id,
                    uri=source.uri,
                    quote=cit_out.quote,
                )
            )

    answer = Answer(
        text=answer_out.text,
        citations=citations,
        confidence=answer_out.confidence,
    )

    return {
        "answer": answer,
        "spent": spent,
        "attempts": {"generate": 1},
    }
