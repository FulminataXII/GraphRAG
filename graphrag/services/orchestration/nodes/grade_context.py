from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from graphrag.core.errors import LLMProviderExhausted, LLMSchemaViolation
from graphrag.core.models import NodeFailure, ScoredChunk, Spend

if TYPE_CHECKING:
    from graphrag.services.orchestration.graph import NodeDeps
from graphrag.services.orchestration.prompts import render
from graphrag.services.orchestration.schemas import RelevanceGradeBatch
from graphrag.services.orchestration.state import QueryState

#: `degraded` marker for a grader that could not grade. One string for the whole node, not one
#: per batch: the channel is `Annotated[list[str], operator.add]`, so appending per batch would
#: make a two-batch query read as twice as broken as a one-batch one.
DEGRADED = "grader"


async def grade_batch(
    chunks: list[ScoredChunk], state: QueryState, deps: NodeDeps
) -> tuple[list[ScoredChunk], Spend, NodeFailure | None]:
    """Grade one batch. Returns the survivors, what it cost, and a failure if it could not grade.

    Failing open is deliberate (`orchestration.grader.fail_open`): a broken grader should not
    take the query down. Failing open SILENTLY is the bug this third return value fixes — every
    chunk passes through ungraded, `orchestration.min_relevant_docs` is trivially satisfied,
    `route_after_grade` never reaches `rewrite_query`, and the answer is indistinguishable from
    one whose context really was relevant.
    """
    question = state["question"]
    prompt = render("grade_context.j2", question=question, chunks=[c.chunk for c in chunks])
    attempt = state.get("attempts", {}).get("grade_context", 0) + 1

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
        # A chunk the grader did not mention is still dropped, unchanged from before. A
        # truncated-but-parseable response therefore loses chunks silently — `grader.max_tokens`
        # bounds the whole completion and this is a reasoning model, so a batch really can come
        # back as valid JSON carrying grades for only the first few chunks (measured: at
        # max_tokens 600, finish_reason=length with 2 grades for 8 chunks). Reported rather than
        # handled here: deciding whether an omitted grade means "irrelevant" or "not graded"
        # changes what `grade_context` returns and is a BLUEPRINT §6.4 semantics call.
        relevant_ids = {g.chunk_id for g in result.value.grades if g.relevant}
        relevant_chunks = [c for c in chunks if str(c.chunk.chunk_id) in relevant_ids]
        return relevant_chunks, spent, None
    except (LLMSchemaViolation, LLMProviderExhausted) as e:
        if not fail_open:
            raise
        deps.metrics.grader_degraded.add(1)
        # spent is 0 in case of failure or whatever was consumed.
        return (
            list(chunks),
            Spend(llm_calls=1),
            NodeFailure(
                node="grade_context",
                code=e.code,
                message=str(e),
                attempt=attempt,
                at=deps.clock.now(),
            ),
        )


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
    failures = []
    total_spent = Spend()
    for batch_graded, batch_spent, batch_failure in results:
        graded.extend(batch_graded)
        total_spent = Spend.merge(total_spent, batch_spent)
        if batch_failure is not None:
            failures.append(batch_failure)

    update: dict[str, Any] = {
        "graded": graded,
        "spent": total_spent,
        "attempts": {"grade_context": 1},
    }
    if failures:
        # Every failing batch is listed (the count is what tells BO-11 how much of the context
        # went ungraded), but `degraded` carries one marker for the node.
        update["failures"] = failures
        update["degraded"] = [DEGRADED]
    return update
