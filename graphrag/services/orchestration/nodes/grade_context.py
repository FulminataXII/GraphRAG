from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from graphrag.core.errors import LLMProviderExhausted, LLMSchemaViolation
from graphrag.core.models import ScoredChunk, Spend

if TYPE_CHECKING:
    from graphrag.services.orchestration.graph import NodeDeps
from graphrag.services.orchestration.prompts import render
from graphrag.services.orchestration.schemas import RelevanceGradeBatch
from graphrag.services.orchestration.state import QueryState


async def grade_batch(
    chunks: list[ScoredChunk], state: QueryState, deps: NodeDeps
) -> tuple[list[ScoredChunk], Spend]:
    question = state["question"]
    prompt = render("grade_context.j2", question=question, chunks=[c.chunk for c in chunks])

    fail_open = deps.settings.orchestration.grader.fail_open
    try:
        result = await deps.llm.structured(
            role="grader",
            messages=[{"role": "user", "content": prompt}],
            schema=RelevanceGradeBatch,
            max_repairs=deps.settings.orchestration.max_structured_output_repairs,
        )
        spent = Spend(
            llm_calls=result.repair_attempts + 1,
            tokens=result.prompt_tokens + result.completion_tokens,
            wall_ms=result.latency_ms,
        )
        relevant_ids = {g.chunk_id for g in result.value.grades if g.relevant}
        relevant_chunks = [c for c in chunks if str(c.chunk.chunk_id) in relevant_ids]
        return relevant_chunks, spent
    except (LLMSchemaViolation, LLMProviderExhausted) as e:
        if fail_open:
            deps.metrics.grader_degraded.add(1)
            # spent is 0 in case of failure or whatever was consumed.
            # We can just return the chunks and an empty spend for simplicity if it fails open.
            return list(chunks), Spend(llm_calls=1)
        raise e


async def node(state: QueryState, deps: NodeDeps) -> dict[str, Any]:
    fused = state.get("fused", [])
    if not fused:
        return {"graded": [], "attempts": {"grade_context": 1}}

    batch_size = deps.settings.orchestration.grader.batch_size
    tasks = []
    for i in range(0, len(fused), batch_size):
        batch = fused[i : i + batch_size]
        tasks.append(grade_batch(batch, state, deps))

    results = await asyncio.gather(*tasks)

    graded = []
    total_spent = Spend()
    for batch_graded, batch_spent in results:
        graded.extend(batch_graded)
        total_spent = Spend.merge(total_spent, batch_spent)

    return {
        "graded": graded,
        "spent": total_spent,
        "attempts": {"grade_context": 1},
    }
