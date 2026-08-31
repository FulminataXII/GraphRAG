"""`ResolutionService`, `ResolutionResult`. See BLUEPRINT §6.2.

`ResolutionResult` is a spec gap BLUEPRINT §1a names explicitly: its module and owning BO are
listed in the Type Index, but "its field list is only implied by the `resolve()` contract" — the
Type Index says to report this rather than invent, which this docstring does; the fields below
are the reported resolution: `entities`, `aliases` (loser -> canonical, per the `resolve()`
contract's own wording), and `flagged` (gray-band pairs), matching exactly the three things
`resolve()`'s docstring says it returns.

Two more judgment calls, both reported in the BO-07 summary rather than silently assumed:

1. Mentions sharing an identical (normalized_name, type) are grouped before blocking/scoring —
   BLUEPRINT's pipeline is written per-mention, but two mentions that normalize identically are
   the same match by definition, and grouping first avoids redundant O(duplicates^2) scoring
   work for a name that simply appears many times in a corpus (e.g. "Apple").
2. `Blocker.candidates()` returns plain `Entity` objects (per its own BLUEPRINT signature), which
   drops the similarity score Qdrant computed for the vector leg. Recomputing cosine similarity
   client-side (`blocking.cosine_similarity`) from cached name embeddings is the least invasive
   way to recover a `cosine` value for `score_pair()`'s vector-leg candidates without changing
   `Blocker`'s contracted return type.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from graphrag.core.ids import entity_id
from graphrag.core.models import Entity
from graphrag.services.resolution.blocking import Blocker, cosine_similarity
from graphrag.services.resolution.clustering import choose_canonical, cluster
from graphrag.services.resolution.normalize import normalize_entity_name
from graphrag.services.resolution.scoring import decide, score_pair

if TYPE_CHECKING:
    from graphrag.config.schema import ResolutionSection
    from graphrag.core.models import EntityType, Mention
    from graphrag.core.ports import Embedder


class AliasEdge(BaseModel):
    """A resolved alias -> canonical edge. Shape matches `GraphStore.add_alias`'s parameters
    (BO-08 wires the actual write; BO-07 only produces the edges)."""

    model_config = ConfigDict(frozen=True)

    alias_id: UUID
    canonical_id: UUID
    score: float
    method: Literal["cluster"]


class FlaggedPair(BaseModel):
    """A gray-band pair recorded but not merged (`gray_band_action='flag'`)."""

    model_config = ConfigDict(frozen=True)

    a_id: UUID
    b_id: UUID
    score: float


class ResolutionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    entities: list[Entity]
    aliases: list[AliasEdge]
    flagged: list[FlaggedPair]


class ResolutionService:
    """Contract of resolve(mentions) -> ResolutionResult:
    normalize -> block -> score -> decide -> cluster -> choose canonical
    - Emits entities_merged{band} metrics for merge/gray/reject.
    - gray_band_action='flag' records the pair in the result but does NOT merge.
    - Idempotent over the same corpus: zero new canonical_ids on a re-run.
    - Returns entities, alias edges (loser -> canonical, never deleted), and flagged pairs.
    """

    def __init__(
        self,
        *,
        blocker: Blocker,
        embedder: Embedder,
        resolution: ResolutionSection,
        # `Metrics` has no `core.ports` Protocol (services/ may not import adapters/); typed Any
        # for the same reason `IngestionService.metrics` is, see that module's docstring.
        metrics: Any,
    ) -> None:
        self._blocker = blocker
        self._embedder = embedder
        self._resolution = resolution
        self._metrics = metrics

    async def resolve(self, mentions: Sequence[Mention]) -> ResolutionResult:
        if not mentions:
            return ResolutionResult(entities=[], aliases=[], flagged=[])

        groups: dict[tuple[str, EntityType], list[Mention]] = {}
        for mention in mentions:
            normalized = normalize_entity_name(
                mention.surface,
                strip_suffixes=self._resolution.strip_suffixes,
                strip_honorifics=self._resolution.strip_honorifics,
            )
            groups.setdefault((normalized, mention.type), []).append(mention)

        group_keys = sorted(groups.keys(), key=lambda k: (k[0], k[1].value))
        group_vectors = await self._embedder.embed_dense([name for name, _type in group_keys])
        # Keyed on THIS group's own choose_canonical(), not the normalized name — so that a
        # group which never merges with anything gets a provisional id that IS its eventual
        # canonical_id (the singleton branch below computes canonical_id the exact same way),
        # instead of a throwaway id that would only ever appear in Blocker's in-batch index and
        # never match what actually gets persisted to VectorStore. A mismatch there broke
        # idempotency: a later run's Blocker would hold two different ids for the "same" entity
        # (a stale local one and the real persisted one) and could pick the wrong one as winner.
        group_id_of: dict[tuple[str, EntityType], UUID] = {
            key: entity_id(choose_canonical(members), key[1].value)
            for key, members in groups.items()
        }
        id_to_group = {v: k for k, v in group_id_of.items()}

        vector_of: dict[str, list[float]] = {
            key[0]: vec for key, vec in zip(group_keys, group_vectors, strict=True)
        }

        async def vector_for(name: str) -> list[float]:
            if name not in vector_of:
                [vec] = await self._embedder.embed_dense([name])
                vector_of[name] = vec
            return vector_of[name]

        merge_edges: list[tuple[UUID, UUID]] = []
        flagged: list[FlaggedPair] = []
        candidate_entities_by_id: dict[UUID, Entity] = {}

        for key, vector in zip(group_keys, group_vectors, strict=True):
            normalized, entity_type = key
            gid = group_id_of[key]

            candidates = await self._blocker.candidates(normalized, entity_type, vector)
            for candidate in candidates:
                candidate_vector = await vector_for(candidate.name_normalized)
                cosine = cosine_similarity(vector, candidate_vector)
                score = score_pair(
                    normalized,
                    candidate.name_normalized,
                    entity_type,
                    candidate.type,
                    cosine,
                    self._resolution.scorer_weights,
                    self._resolution.require_type_match,
                )
                band = decide(
                    score,
                    self._resolution.auto_merge_threshold,
                    self._resolution.auto_reject_threshold,
                )
                self._metrics.entities_merged.add(1, {"band": band})

                if band == "merge":
                    merge_edges.append((gid, candidate.canonical_id))
                    candidate_entities_by_id.setdefault(candidate.canonical_id, candidate)
                elif band == "gray" and self._resolution.gray_band_action == "flag":
                    flagged.append(FlaggedPair(a_id=gid, b_id=candidate.canonical_id, score=score))

            group_members = groups[key]
            self._blocker.index(
                [
                    Entity(
                        canonical_id=gid,
                        name=choose_canonical(group_members),
                        name_normalized=normalized,
                        type=entity_type,
                        aliases=[],
                        mention_count=len(group_members),
                    )
                ],
                [vector],
            )

        clusters = cluster(merge_edges, max_cluster_size=self._resolution.max_cluster_size)
        clustered_ids = {uid for members in clusters for uid in members}

        entities: list[Entity] = []
        aliases: list[AliasEdge] = []

        for members in clusters:
            member_mentions: list[Mention] = []
            member_candidates: list[Entity] = []
            for uid in members:
                group_key = id_to_group.get(uid)
                if group_key is not None:
                    member_mentions.extend(groups[group_key])
                existing_candidate = candidate_entities_by_id.get(uid)
                if existing_candidate is not None:
                    member_candidates.append(existing_candidate)

            if member_candidates:
                # An already-published canonical_id in this cluster MUST win — reassigning it
                # would orphan its existing alias edges and mint a spurious new canonical_id on
                # every re-run, breaking idempotency. Deterministic tie-break for the rare case
                # of several already-published entities merging together in one run.
                winner = min(member_candidates, key=lambda e: e.canonical_id)
                canonical_id = winner.canonical_id
                canonical_type = winner.type
                canonical_name = (
                    choose_canonical(member_mentions) if member_mentions else winner.name
                )
            else:
                canonical_name = choose_canonical(member_mentions)
                canonical_type = member_mentions[0].type
                canonical_id = entity_id(canonical_name, canonical_type.value)

            surfaces = {m.surface for m in member_mentions} | {e.name for e in member_candidates}
            alias_surfaces = sorted(surfaces - {canonical_name})
            mention_count = len(member_mentions) + sum(e.mention_count for e in member_candidates)

            entities.append(
                Entity(
                    canonical_id=canonical_id,
                    name=canonical_name,
                    name_normalized=normalize_entity_name(
                        canonical_name,
                        strip_suffixes=self._resolution.strip_suffixes,
                        strip_honorifics=self._resolution.strip_honorifics,
                    ),
                    type=canonical_type,
                    aliases=alias_surfaces,
                    mention_count=mention_count,
                )
            )
            for alias_surface in alias_surfaces:
                # Keyed on the alias SURFACE STRING, not the losing group's own `gid` — a group
                # can itself contain several distinct raw surfaces that all normalize identically
                # (see the "for key in group_keys" singleton branch below, which shares this
                # scheme), so one alias edge per surface is what keeps both branches consistent.
                aliases.append(
                    AliasEdge(
                        alias_id=entity_id(alias_surface, canonical_type.value),
                        canonical_id=canonical_id,
                        score=1.0,
                        method="cluster",
                    )
                )

        for key in group_keys:
            gid = group_id_of[key]
            if gid in clustered_ids:
                continue
            member_mentions = groups[key]
            canonical_name = choose_canonical(member_mentions)
            canonical_type = key[1]
            # `gid` already equals entity_id(canonical_name, type) — see group_id_of's
            # construction above — so an unmerged group's provisional id IS its final one.
            canonical_id = gid
            alias_surfaces = sorted({m.surface for m in member_mentions} - {canonical_name})
            for alias_surface in alias_surfaces:
                aliases.append(
                    AliasEdge(
                        alias_id=entity_id(alias_surface, canonical_type.value),
                        canonical_id=canonical_id,
                        score=1.0,
                        method="cluster",
                    )
                )
            entities.append(
                Entity(
                    canonical_id=canonical_id,
                    name=canonical_name,
                    name_normalized=key[0],
                    type=canonical_type,
                    aliases=alias_surfaces,
                    mention_count=len(member_mentions),
                )
            )

        return ResolutionResult(entities=entities, aliases=aliases, flagged=flagged)


__all__ = ["AliasEdge", "FlaggedPair", "ResolutionResult", "ResolutionService"]
