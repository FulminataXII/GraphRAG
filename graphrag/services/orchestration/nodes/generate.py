from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Final

from graphrag.core.models import Answer, Citation, Spend

if TYPE_CHECKING:
    from graphrag.services.orchestration.graph import NodeDeps
from graphrag.services.orchestration.prompts import render
from graphrag.services.orchestration.schemas import AnswerOut
from graphrag.services.orchestration.state import QueryState

#: Stands in for a cited id that is not a UUID at all — see `node`.
_UNRESOLVABLE_CHUNK_ID: Final[uuid.UUID] = uuid.UUID(int=0)


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

    # EVERY citation the model emits is carried through, including ones this node cannot
    # resolve. BLUEPRINT §6.4 makes `verify_citations` the deterministic guard against a
    # fabricated identifier ("the right handling is a deterministic membership check"), and a
    # guard can only adjudicate what reaches it: filtering unresolvable ids out here leaves
    # verify_citations validating the survivors against the very set they were selected from,
    # which it can never find a violation in. The answer text meanwhile keeps the claim the
    # dropped citation was supposed to support, so the filtering loses the evidence without
    # losing the assertion.
    chunk_map = {str(c.chunk.chunk_id): c.chunk for c in graded}

    citations = []
    for cit_out in answer_out.citations:
        chunk = chunk_map.get(cit_out.chunk_id)
        try:
            cid = uuid.UUID(cit_out.chunk_id)
        except ValueError:
            # Not even a UUID. `Citation.chunk_id` is typed `UUID` and cannot carry the raw
            # text, so it maps to the nil UUID — which is not a content-addressed chunk id
            # (those are sha256 digest slices) and therefore can never be in `graded`.
            # verify_citations rejects it on the same membership check as any other
            # hallucinated id, rather than this node raising and burning a repair attempt.
            cid = _UNRESOLVABLE_CHUNK_ID
        # Provenance comes from the chunk, so an unresolvable citation has none to report. It
        # never reaches a client: verify_citations fails the answer, and the repair loop either
        # replaces it or `insufficient` does.
        source = chunk.sources[0] if chunk and chunk.sources else None
        citations.append(
            Citation(
                chunk_id=cid,
                doc_id=source.doc_id if source is not None else "",
                uri=source.uri if source is not None else "",
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
