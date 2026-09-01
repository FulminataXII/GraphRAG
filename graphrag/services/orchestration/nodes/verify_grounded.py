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
    if not answer:
        return {}

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
                "spent": spent,
                "failures": [
                    NodeFailure(
                        node="verify_grounded",
                        code="UNGROUNDED_ANSWER",
                        message=f"Answer is not grounded (score {entailment.score})",
                        attempt=1,
                        at=deps.clock.now(),
                    )
                ],
            }

        return {"spent": spent}
    except LLMSchemaViolation as e:
        return {
            "failures": [
                NodeFailure(
                    node="verify_grounded",
                    code=e.code,
                    message=str(e),
                    attempt=1,
                    at=deps.clock.now(),
                )
            ]
        }
