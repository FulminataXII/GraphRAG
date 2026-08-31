"""`resolve_entities` arq task. See BLUEPRINT §7.2 / §6.2.

Persists `ResolutionService.resolve()`'s output to Qdrant (`VectorStore.upsert_entities`) AND, as
of BO-08, to Neo4j: `GraphStore.upsert_entities`, `GraphStore.add_alias` for every
`ResolutionResult.aliases` edge, and `GraphStore.upsert_relations` for the relations
`extract_entities` carried across the queue (see `core.events.UnresolvedRelation`) — this is
where BO-08's "Wire graph writes into IngestionService / resolve task" build step lands for the
resolve side. Mirrors the established pattern of guarding on `graph_store is not None` (e.g.
`IngestionService`'s own graph writes) rather than assuming it is always configured.

As of BO-09 item 0, also calls `GraphStore.upsert_mentions` for every `env.payload.mentions`
entry that resolves to a canonical entity — this is what keeps an entity reachable from its
source chunk when it never participates in a relation (`extract_entities` emits mentions for
every extracted entity, but relations only for entity PAIRS the LLM connected; a singleton
mention has no relation to carry chunk_id/doc_id provenance through, so without this call it
would be upserted to Qdrant's entities collection but never linked back to graph-side chunk
text).

Both a relation's `src_surface`/`dst_surface` and a mention's `surface` become a canonical
`UUID` the same way: looked up against a surface -> canonical_id map built from THIS batch's
`ResolutionResult.entities` (their `name` and every `aliases` entry — the exact raw surface
strings the cluster was built from, see `ResolutionService.resolve()`). A surface that doesn't
resolve — the LLM hallucinated an endpoint, or the entity landed in a different batch — is
dropped, the same way a hallucinated `chunk_id` is dropped in `extract_entities`, not treated as
an error.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from graphrag.apps.worker.tasks._common import run_task
from graphrag.core.errors import AppError
from graphrag.core.events import JobEnvelope, ResolveEntitiesPayload, UnresolvedRelation
from graphrag.core.models import DocumentStatus, Entity, Mention, Relation
from graphrag.services.resolution.blocking import Blocker
from graphrag.services.resolution.service import ResolutionService


def _surface_to_canonical(entities: Sequence[Entity]) -> dict[str, UUID]:
    mapping: dict[str, UUID] = {}
    for entity in entities:
        mapping[entity.name] = entity.canonical_id
        for alias in entity.aliases:
            mapping[alias] = entity.canonical_id
    return mapping


def _resolve_relations(
    unresolved: Sequence[UnresolvedRelation],
    doc_id: str,
    surface_to_canonical: dict[str, UUID],
) -> list[Relation]:
    relations: list[Relation] = []
    for candidate in unresolved:
        src_id = surface_to_canonical.get(candidate.src_surface)
        dst_id = surface_to_canonical.get(candidate.dst_surface)
        if src_id is None or dst_id is None:
            continue  # endpoint surface didn't resolve to a known entity -- drop, don't error
        relations.append(
            Relation(
                src_id=src_id,
                dst_id=dst_id,
                type=candidate.type,
                confidence=candidate.confidence,
                chunk_id=candidate.chunk_id,
                doc_id=doc_id,
                evidence_span=candidate.evidence_span,
            )
        )
    return relations


def _link_mentions(
    mentions: Sequence[Mention], surface_to_canonical: dict[str, UUID]
) -> list[Mention]:
    """Every mention that resolves gets its `entity_id` filled in via `model_copy` (`Mention` is
    frozen) — `GraphStore.upsert_mentions` (BLUEPRINT §3.5) rejects a null `entity_id`, so an
    unresolved mention must never reach it in the first place."""
    linked: list[Mention] = []
    for mention in mentions:
        canonical_id = surface_to_canonical.get(mention.surface)
        if canonical_id is None:
            continue  # same drop-not-error handling as _resolve_relations
        linked.append(mention.model_copy(update={"entity_id": canonical_id}))
    return linked


async def resolve_entities(ctx: dict[str, Any], env: JobEnvelope[ResolveEntitiesPayload]) -> None:
    """See `ingest_document`'s docstring for the contract shared by every task in this package."""
    container = ctx["container"]

    async def _on_failure(exc: BaseException) -> None:
        code = exc.code if isinstance(exc, AppError) else "INTERNAL_ERROR"
        await container.ledger.set_status(
            env.payload.doc_id, DocumentStatus.FAILED, error_code=code
        )

    async def _body() -> None:
        blocker = Blocker(
            vector_store=container.vector_store, resolution=container.settings.resolution
        )
        service = ResolutionService(
            blocker=blocker,
            embedder=container.embedder,
            resolution=container.settings.resolution,
            metrics=container.metrics,
        )
        result = await service.resolve(env.payload.mentions)

        if result.entities:
            vectors = await container.embedder.embed_dense(
                [entity.name_normalized for entity in result.entities]
            )
            await container.vector_store.upsert_entities(result.entities, vectors)

        if container.graph_store is not None:
            if result.entities:
                await container.graph_store.upsert_entities(result.entities)
            for alias in result.aliases:
                await container.graph_store.add_alias(
                    alias.alias_id, alias.canonical_id, alias.score, alias.method
                )
            surface_to_canonical = _surface_to_canonical(result.entities)
            if env.payload.mentions:
                linked_mentions = _link_mentions(env.payload.mentions, surface_to_canonical)
                if linked_mentions:
                    await container.graph_store.upsert_mentions(linked_mentions)
            if env.payload.relations:
                relations = _resolve_relations(
                    env.payload.relations, env.payload.doc_id, surface_to_canonical
                )
                if relations:
                    await container.graph_store.upsert_relations(relations)

        # Forward-only, like `extract_entities`' own guard — see that module for why.
        record = await container.ledger.get(env.payload.doc_id)
        if record is not None and record.status == DocumentStatus.RESOLVING:
            await container.ledger.set_status(env.payload.doc_id, DocumentStatus.INDEXED)
            await container.ledger.bump_corpus_version()

    await run_task("resolve_entities", ctx, env, _body, on_failure=_on_failure)


__all__ = ["resolve_entities"]
