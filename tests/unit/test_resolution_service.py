"""`ResolutionService` unit tests. See BLUEPRINT §6.2 / BUILD_ORDER BO-07.

Uses a small scripted `Embedder` double (not `tests.fakes.FakeEmbedder`, whose SHA-256-of-the-
whole-string vectors carry no controllable similarity relationship between two chosen strings)
so the gray-band test can land a pair at a KNOWN score deterministically, without touching
`resolution.auto_merge_threshold`/`auto_reject_threshold` themselves — those stay at their
configured values throughout, per the BO-07 instructions against hand-tuning config.
"""

from __future__ import annotations

import math
from uuid import uuid4

from graphrag.config.schema import ResolutionSection
from graphrag.core.models import EntityType, Mention
from graphrag.services.resolution.blocking import Blocker
from graphrag.services.resolution.normalize import normalize_entity_name
from graphrag.services.resolution.scoring import score_pair
from graphrag.services.resolution.service import ResolutionService


class _EmptyVectorStore:
    """No persisted entities — every test here is about in-batch behaviour only."""

    async def search_entities(self, vector, *, top_k, entity_type):
        return []


class _ScriptedEmbedder:
    """Returns a pre-registered unit vector per (already-normalized) text. Raises if asked to
    embed a text this test didn't anticipate, so a silent gap can't hide as a zero vector."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors
        self.dimensions = 2

    async def embed_dense(self, texts, *, is_query: bool = False) -> list[list[float]]:
        return [self._vectors[text] for text in texts]

    async def embed_sparse(self, texts):
        raise NotImplementedError


def _unit_vector(cosine_to_first_axis: float) -> list[float]:
    return [cosine_to_first_axis, math.sqrt(max(0.0, 1.0 - cosine_to_first_axis**2))]


def _mention(surface: str, entity_type: EntityType = EntityType.ORG) -> Mention:
    return Mention(
        surface=surface,
        type=entity_type,
        chunk_id=uuid4(),
        char_start=0,
        char_end=len(surface),
        confidence=0.9,
    )


def _metrics() -> object:
    calls: list[tuple[int, dict | None]] = []

    class _Counter:
        def add(self, amount, attributes=None):
            calls.append((amount, attributes))

    class _Metrics:
        entities_merged = _Counter()

    metrics = _Metrics()
    metrics.calls = calls  # type: ignore[attr-defined]
    return metrics


async def test_resolve_empty_mentions_returns_empty_result(settings) -> None:
    blocker = Blocker(vector_store=_EmptyVectorStore(), resolution=settings.resolution)
    service = ResolutionService(
        blocker=blocker,
        embedder=_ScriptedEmbedder({}),
        resolution=settings.resolution,
        metrics=_metrics(),
    )
    result = await service.resolve([])
    assert result.entities == []
    assert result.aliases == []
    assert result.flagged == []


async def test_gray_band_not_auto_merged(settings) -> None:
    """A 0.75-scoring pair (config thresholds: reject <= 0.65 < gray < 0.90 <= merge) stays
    distinct and is flagged, never merged — `gray_band_action='flag'` per config."""
    resolution: ResolutionSection = settings.resolution
    assert resolution.gray_band_action == "flag"  # sanity: config wasn't tuned for this test

    a_surface, b_surface = "Widget Systems", "Widget Solutions"
    a_norm = normalize_entity_name(
        a_surface,
        strip_suffixes=resolution.strip_suffixes,
        strip_honorifics=resolution.strip_honorifics,
    )
    b_norm = normalize_entity_name(
        b_surface,
        strip_suffixes=resolution.strip_suffixes,
        strip_honorifics=resolution.strip_honorifics,
    )

    # Solve for the cosine that lands score_pair() at exactly 0.75 given the REAL (untuned)
    # jaro_winkler/token_set_ratio for these two normalized names and the configured weights —
    # not a magic number, so a future weights/threshold edit fails this test loudly instead of
    # silently drifting out of the gray band.
    vec_a = _unit_vector(1.0)
    target_score = 0.75
    assert resolution.auto_reject_threshold < target_score < resolution.auto_merge_threshold
    probe = score_pair(
        a_norm, b_norm, EntityType.ORG, EntityType.ORG, 0.0, resolution.scorer_weights, True
    )
    non_cosine_component = probe  # cosine=0 contributes nothing, isolating the other two terms
    needed_cosine = (
        target_score - non_cosine_component
    ) / resolution.scorer_weights.embedding_cosine
    vec_b = _unit_vector(needed_cosine)

    embedder = _ScriptedEmbedder({a_norm: vec_a, b_norm: vec_b})
    blocker = Blocker(vector_store=_EmptyVectorStore(), resolution=resolution)
    metrics = _metrics()
    service = ResolutionService(
        blocker=blocker, embedder=embedder, resolution=resolution, metrics=metrics
    )

    mentions = [_mention(a_surface), _mention(b_surface)]
    result = await service.resolve(mentions)

    assert len(result.entities) == 2  # both stay distinct
    assert result.aliases == []  # no alias edge — nothing was merged
    assert len(result.flagged) == 1
    flagged = result.flagged[0]
    assert resolution.auto_reject_threshold < flagged.score < resolution.auto_merge_threshold
    assert any(attrs == {"band": "gray"} for _amount, attrs in metrics.calls)


async def test_high_score_pair_merges_into_one_entity(settings) -> None:
    resolution: ResolutionSection = settings.resolution
    a_surface, b_surface = "Widget Systems", "Widget Systems Group"
    a_norm = normalize_entity_name(
        a_surface,
        strip_suffixes=resolution.strip_suffixes,
        strip_honorifics=resolution.strip_honorifics,
    )
    b_norm = normalize_entity_name(
        b_surface,
        strip_suffixes=resolution.strip_suffixes,
        strip_honorifics=resolution.strip_honorifics,
    )
    embedder = _ScriptedEmbedder({a_norm: _unit_vector(1.0), b_norm: _unit_vector(1.0)})
    blocker = Blocker(vector_store=_EmptyVectorStore(), resolution=resolution)
    service = ResolutionService(
        blocker=blocker, embedder=embedder, resolution=resolution, metrics=_metrics()
    )

    result = await service.resolve([_mention(a_surface), _mention(b_surface)])

    assert len(result.entities) == 1
    assert len(result.aliases) == 1
    assert result.aliases[0].canonical_id == result.entities[0].canonical_id


async def test_three_variants_one_group_produces_two_alias_edges(settings) -> None:
    """`"Acme Corp." / "ACME Corporation" / "Acme"` normalize identically (all -> "acme"), so
    they fall into ONE group before any blocking/scoring ever runs. This is the unit-level
    sibling of the integration-marked `test_three_variants_merge` gate (which additionally
    exercises the real Qdrant `entities` collection) — it caught a real bug where a single
    group's non-canonical surfaces were silently dropped instead of becoming alias edges."""
    resolution = settings.resolution
    norm = normalize_entity_name(
        "Acme",
        strip_suffixes=resolution.strip_suffixes,
        strip_honorifics=resolution.strip_honorifics,
    )
    embedder = _ScriptedEmbedder({norm: _unit_vector(1.0)})
    blocker = Blocker(vector_store=_EmptyVectorStore(), resolution=resolution)
    service = ResolutionService(
        blocker=blocker, embedder=embedder, resolution=resolution, metrics=_metrics()
    )

    mentions = [_mention("Acme Corp."), _mention("ACME Corporation"), _mention("Acme")]
    result = await service.resolve(mentions)

    assert len(result.entities) == 1
    entity = result.entities[0]
    assert entity.name == "ACME Corporation"  # longest surface, tied on frequency
    assert set(entity.aliases) == {"Acme Corp.", "Acme"}
    assert len(result.aliases) == 2
    assert all(edge.canonical_id == entity.canonical_id for edge in result.aliases)


async def test_resolve_is_idempotent_within_a_single_call_for_repeated_surfaces(settings) -> None:
    """Same surface mentioned many times collapses into one entity with the right mention_count,
    not one entity per occurrence."""
    resolution = settings.resolution
    norm = normalize_entity_name(
        "Acme",
        strip_suffixes=resolution.strip_suffixes,
        strip_honorifics=resolution.strip_honorifics,
    )
    embedder = _ScriptedEmbedder({norm: _unit_vector(1.0)})
    blocker = Blocker(vector_store=_EmptyVectorStore(), resolution=resolution)
    service = ResolutionService(
        blocker=blocker, embedder=embedder, resolution=resolution, metrics=_metrics()
    )

    mentions = [_mention("Acme") for _ in range(5)]
    result = await service.resolve(mentions)

    assert len(result.entities) == 1
    assert result.entities[0].mention_count == 5
