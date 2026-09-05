from __future__ import annotations

from typing import TYPE_CHECKING, Any

from graphrag.core.errors import LLMSchemaViolation
from graphrag.core.models import NodeFailure, Spend

if TYPE_CHECKING:
    from graphrag.services.orchestration.graph import NodeDeps
from graphrag.services.orchestration.prompts import render
from graphrag.services.orchestration.schemas import Entailment
from graphrag.services.orchestration.state import QueryState


async def node(state: QueryState, deps: NodeDeps) -> dict[str, Any]:
    answer = state.get("answer")
    graded = state.get("graded", [])
    # 1-based index of THIS run of this node. `failures` is append-only (BLUEPRINT §6.4), so a
    # NodeFailure is only distinguishable from an earlier run's by the attempt it was recorded
    # on -- see `graph._failed_on_current_attempt`. Counted on every path, including the ones
    # that do no work: the number must track node executions or it mislabels a later failure.
    attempt = state.get("attempts", {}).get("verify_grounded", 0) + 1
    counted = {"attempts": {"verify_grounded": 1}}
    if not answer:
        return counted

    chunks = [c.chunk for c in graded]

    prompt = render("verify_grounded.j2", answer=answer.text, chunks=chunks)

    try:
        result = await deps.llm.structured(
            role="judge",
            messages=[{"role": "user", "content": prompt}],
            schema=Entailment,
            max_repairs=deps.settings.orchestration.max_structured_output_repairs,
        )
        spent = Spend(
            llm_calls=result.repair_attempts + 1,
            tokens=result.prompt_tokens + result.completion_tokens,
            wall_ms=result.latency_ms,
        )
        entailment = result.value

        if entailment.score < deps.settings.orchestration.verification.min_groundedness_score:
            return {
                **counted,
                "spent": spent,
                "failures": [
                    NodeFailure(
                        node="verify_grounded",
                        code="UNGROUNDED_ANSWER",
                        message=f"Answer is not grounded (score {entailment.score})",
                        attempt=attempt,
                        at=deps.clock.now(),
                    )
                ],
            }

        return {**counted, "spent": spent}
    except LLMSchemaViolation as e:
        return {
            **counted,
            "failures": [
                NodeFailure(
                    node="verify_grounded",
                    code=e.code,
                    message=str(e),
                    attempt=attempt,
                    at=deps.clock.now(),
                )
            ],
        }
