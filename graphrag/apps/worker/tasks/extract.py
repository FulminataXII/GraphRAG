"""`extract_entities` arq task. See BLUEPRINT §7.2 / ARCHITECTURE §4.3.

Extraction is real quota: the `bulk` role (Gemini Flash-Lite per ARCHITECTURE §4.3) is
RPM-bound, not TPM-bound, so this batches `llm.batching.bulk_chunks_per_request` chunks into
ONE `LLMClient.structured()` call rather than one call per chunk — a one-chunk-per-request
worker stalls at the request ceiling while using a fraction of its token allowance. The batch
size is not tuned here; it is read as-is from config (`llm.batching.bulk_chunks_per_request`,
20 in `config.example.yaml`) per the BO-07 instructions against hand-tuning config values.

`MentionOut.chunk_id` / `RelationOut.chunk_id` (added in `services/orchestration/schemas.py`,
see that module for why) are validated the same way `verify_citations` validates
`AnswerOut.citations[].chunk_id` (BLUEPRINT §6.4): a hallucinated id is a deterministic
membership check against the batch's real chunk ids, not a parse failure that burns a repair
attempt. A mention whose `char_start`/`char_end` falls outside its claimed chunk's text is
dropped the same way — this is `test_extraction_spans_within_chunk`'s guarantee. A relation has
no offsets to check (`RelationOut` carries no `char_start`/`char_end`), only the same chunk_id
membership check.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from graphrag.adapters.clock import SystemClock
from graphrag.apps.worker.tasks._common import run_task
from graphrag.core.errors import AppError
from graphrag.core.events import (
    ExtractEntitiesPayload,
    JobEnvelope,
    ResolveEntitiesPayload,
    UnresolvedRelation,
)
from graphrag.core.models import DocumentStatus, Mention
from graphrag.services.orchestration.prompts import render
from graphrag.services.orchestration.schemas import EntityExtraction, MentionOut, RelationOut

if TYPE_CHECKING:
    from graphrag.core.models import Chunk
    from graphrag.core.ports import LLMClient


def _batched(items: Sequence[Chunk], size: int) -> list[Sequence[Chunk]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _valid_mention(mention_out: MentionOut, chunks_by_id: dict[str, Chunk]) -> Mention | None:
    chunk = chunks_by_id.get(mention_out.chunk_id)
    if chunk is None:
        return None  # hallucinated chunk_id -- not one of the ids given in the prompt
    if not (0 <= mention_out.char_start < mention_out.char_end <= len(chunk.text)):
        return None  # offsets don't lie inside the chunk they're claimed to belong to
    return Mention(
        surface=mention_out.surface,
        type=mention_out.type,
        chunk_id=chunk.chunk_id,
        char_start=mention_out.char_start,
        char_end=mention_out.char_end,
        confidence=mention_out.confidence,
    )


def _valid_relation(
    relation_out: RelationOut, chunks_by_id: dict[str, Chunk]
) -> UnresolvedRelation | None:
    chunk = chunks_by_id.get(relation_out.chunk_id)
    if chunk is None:
        return None  # hallucinated chunk_id -- not one of the ids given in the prompt
    return UnresolvedRelation(
        chunk_id=chunk.chunk_id,
        src_surface=relation_out.src_surface,
        dst_surface=relation_out.dst_surface,
        type=relation_out.type,
        confidence=relation_out.confidence,
        evidence_span=relation_out.evidence_span,
    )


async def extract_mentions(
    chunks: Sequence[Chunk],
    *,
    llm_client: LLMClient,
    batch_size: int,
    max_repairs: int,
) -> tuple[list[Mention], list[UnresolvedRelation]]:
    """LLM-extracts entity mentions AND relations from `chunks`, batched at `batch_size` per
    call. Relations come back with surface-form endpoints, not canonical ids — see
    `UnresolvedRelation` for why, and `resolve_entities` for where they get resolved and given a
    sink.
    """
    mentions: list[Mention] = []
    relations: list[UnresolvedRelation] = []
    for batch in _batched(chunks, batch_size):
        chunks_by_id = {str(chunk.chunk_id): chunk for chunk in batch}
        prompt = render(
            "extract_entities.j2",
            chunks=[{"chunk_id": str(chunk.chunk_id), "text": chunk.text} for chunk in batch],
        )
        result = await llm_client.structured(
            role="bulk",
            messages=[{"role": "user", "content": prompt}],
            schema=EntityExtraction,
            max_repairs=max_repairs,
        )
        mentions.extend(
            mention
            for mention_out in result.value.entities
            if (mention := _valid_mention(mention_out, chunks_by_id)) is not None
        )
        relations.extend(
            relation
            for relation_out in result.value.relations
            if (relation := _valid_relation(relation_out, chunks_by_id)) is not None
        )
    return mentions, relations


async def extract_entities(ctx: dict[str, Any], env: JobEnvelope[ExtractEntitiesPayload]) -> None:
    """See `ingest_document`'s docstring for the contract shared by every task in this package."""
    container = ctx["container"]

    async def _on_failure(exc: BaseException) -> None:
        code = exc.code if isinstance(exc, AppError) else "INTERNAL_ERROR"
        await container.ledger.set_status(
            env.payload.doc_id, DocumentStatus.FAILED, error_code=code
        )

    async def _body() -> None:
        chunks = await container.vector_store.get_chunks(env.payload.chunk_ids)
        mentions, relations = await extract_mentions(
            chunks,
            llm_client=container.llm_client,
            batch_size=container.settings.llm.batching.bulk_chunks_per_request,
            max_repairs=container.settings.orchestration.max_structured_output_repairs,
        )
        # Forward-only, like IngestionService's own `_advance`: a retried task that already
        # reached RESOLVING (or beyond) on an earlier attempt must not re-set it, since the real
        # ledger's state machine rejects a backward/no-op-but-illegal transition.
        record = await container.ledger.get(env.payload.doc_id)
        if record is not None and record.status == DocumentStatus.EXTRACTING:
            await container.ledger.set_status(env.payload.doc_id, DocumentStatus.RESOLVING)
        await container.job_queue.enqueue(
            "resolve_entities",
            JobEnvelope(
                correlation_id=env.correlation_id,
                otel={},
                enqueued_at=SystemClock().now(),
                payload=ResolveEntitiesPayload(
                    doc_id=env.payload.doc_id, mentions=mentions, relations=relations
                ),
            ),
        )

    await run_task("extract_entities", ctx, env, _body, on_failure=_on_failure)


__all__ = ["extract_entities", "extract_mentions"]
