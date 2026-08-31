"""`Blocker` unit tests, including the BO-07 blocking-recall gate. See BLUEPRINT §6.2.

`tests.fakes.FakeVectorStore.search_entities` ignores the query vector entirely (it just
returns the first `top_k` entities of the right type in insertion order) — correct for what it
exists to test elsewhere, but it would make a recall test vacuous regardless of whether `Blocker`
is any good. `_CosineVectorStore` below is a small, purpose-built local double (not a mock: real
behaviour, just a minimal one) that actually ranks by cosine similarity, so the recall gate
measures the real risk this stage calls out — do NOT swap in `FakeVectorStore` here.

`_lexical_vector` is a deterministic, offline stand-in for a real semantic embedder (character
trigram bag, hashed and L2-normalized) — `FakeEmbedder`'s SHA-256-of-the-whole-string scheme is
by design NOT locality-preserving (two near-identical strings hash to unrelated digests), so it
cannot stand in for "these two names are similar" the way a real embedder would. This is test
scaffolding only; `graphrag.services.resolution.blocking.Blocker` itself is embedder-agnostic —
it only ever consumes whatever vector it's given.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from uuid import UUID, uuid4

from graphrag.config.schema import ResolutionSection
from graphrag.core.models import Entity, EntityType
from graphrag.services.resolution.blocking import Blocker
from graphrag.services.resolution.normalize import normalize_entity_name

FIXTURE_PATH = Path(__file__).parent / "data" / "resolution_blocking_fixture.json"


def _norm(surface: str, resolution: ResolutionSection) -> str:
    return normalize_entity_name(
        surface,
        strip_suffixes=resolution.strip_suffixes,
        strip_honorifics=resolution.strip_honorifics,
    )


def _lexical_vector(text: str, dims: int = 96) -> list[float]:
    vec = [0.0] * dims
    padded = f"  {text.lower()}  "
    for i in range(len(padded) - 2):
        trigram = padded[i : i + 3]
        digest = hashlib.sha256(trigram.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dims
        vec[idx] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


class _CosineVectorStore:
    """Purpose-built local double — see module docstring. Implements only what `Blocker`
    actually calls (`search_entities`); `register` is test-setup sugar, not a port method."""

    def __init__(self) -> None:
        self._entities: dict[UUID, tuple[Entity, list[float]]] = {}

    def register(self, entity: Entity, vector: list[float]) -> None:
        self._entities[entity.canonical_id] = (entity, vector)

    async def search_entities(
        self, vector: list[float], *, top_k: int, entity_type: EntityType | None
    ) -> list[tuple[Entity, float]]:
        scored = [
            (entity, sum(x * y for x, y in zip(vector, cand_vector, strict=True)))
            for entity, cand_vector in self._entities.values()
            if entity_type is None or entity.type == entity_type
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]


def _load_fixture() -> dict:
    with FIXTURE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def test_blocking_fixture_pairs_are_grounded_in_corpus() -> None:
    """Every surface string in the fixture literally occurs in its cited corpus/ source file,
    checked through the SAME parser production code uses (`DocumentParser`) — not an invented
    string. This is what makes the fixture trustworthy independent of anything the Blocker does.
    """
    from graphrag.services.ingestion.parser import DocumentParser

    corpus_dir = Path(__file__).resolve().parents[2] / "corpus"
    mime_by_ext = {
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pdf": "application/pdf",
    }
    parser = DocumentParser()
    text_cache: dict[str, str] = {}

    def text_of(filename: str) -> str:
        if filename not in text_cache:
            path = corpus_dir / filename
            text_cache[filename] = parser.parse(path.read_bytes(), mime_by_ext[path.suffix]).text
        return text_cache[filename]

    fixture = _load_fixture()
    assert len(fixture["pairs"]) >= 200
    for pair in fixture["pairs"]:
        assert pair["a_surface"] in text_of(pair["a_source"]), pair
        assert pair["b_surface"] in text_of(pair["b_source"]), pair


async def test_blocking_recall(settings) -> None:
    """Labelled 200-pair fixture (derived from the real corpus/ articles, see
    tests/unit/data/resolution_blocking_fixture.json): >=95% of true matches retained by
    candidate generation."""
    fixture = _load_fixture()
    merge_pairs = [pair for pair in fixture["pairs"] if pair["label"] == "merge"]
    resolution = settings.resolution

    store = _CosineVectorStore()
    registered: dict[tuple[str, str], UUID] = {}

    def ensure_registered(surface: str, etype: str) -> UUID:
        key = (_norm(surface, resolution), etype)
        if key not in registered:
            cid = uuid4()
            registered[key] = cid
            store.register(
                Entity(
                    canonical_id=cid,
                    name=surface,
                    name_normalized=key[0],
                    type=EntityType[etype],
                    aliases=[],
                    mention_count=1,
                ),
                _lexical_vector(key[0]),
            )
        return registered[key]

    for pair in merge_pairs:
        ensure_registered(pair["a_surface"], pair["a_type"])
        ensure_registered(pair["b_surface"], pair["b_type"])

    blocker = Blocker(vector_store=store, resolution=resolution)

    retained = 0
    for pair in merge_pairs:
        a_norm = _norm(pair["a_surface"], resolution)
        a_type = EntityType[pair["a_type"]]
        b_id = registered[(_norm(pair["b_surface"], resolution), pair["b_type"])]
        candidates = await blocker.candidates(a_norm, a_type, _lexical_vector(a_norm))
        if any(candidate.canonical_id == b_id for candidate in candidates):
            retained += 1

    recall = retained / len(merge_pairs)
    print(f"blocking recall: {retained}/{len(merge_pairs)} = {recall:.3f}")
    assert recall >= 0.95


async def test_blocking_avoids_quadratic(settings) -> None:
    """1,000 mentions -> total candidate comparisons < n * block_k * 1.2.

    Mirrors how `ResolutionService.resolve()` actually drives `Blocker`: mentions are grouped by
    (normalized_name, type) BEFORE ever calling `candidates()`/`index()` — a name that simply
    recurs in a corpus (as most do) collapses to one group, one blocking call. 1,000 synthetic
    mentions collapsing to 50 distinct names (20 occurrences each) is a realistic duplication
    ratio for a real corpus, not a hand-picked one to dodge the bound; explicitly synthetic per
    the BO-07 instructions, since this test measures comparison COUNT, not match correctness.
    """
    resolution = settings.resolution
    n = 1000
    distinct_names = 50

    store = _CosineVectorStore()
    # A modest, ALREADY-PERSISTED pool the remote leg competes against — independent of `n`.
    for i in range(300):
        name = f"seed-entity-{i}"
        store.register(
            Entity(
                canonical_id=uuid4(),
                name=name,
                name_normalized=name,
                type=EntityType.ORG,
                aliases=[],
                mention_count=1,
            ),
            _lexical_vector(name),
        )

    blocker = Blocker(vector_store=store, resolution=resolution)

    total_comparisons = 0
    for i in range(distinct_names):
        name = f"mention-group-{i}"
        vector = _lexical_vector(name)
        candidates = await blocker.candidates(name, EntityType.ORG, vector)
        total_comparisons += len(candidates)
        blocker.index(
            [
                Entity(
                    canonical_id=uuid4(),
                    name=name,
                    name_normalized=name,
                    type=EntityType.ORG,
                    aliases=[],
                    mention_count=n // distinct_names,
                )
            ],
            [vector],
        )

    bound = n * resolution.block_k * 1.2
    assert total_comparisons <= bound, (
        f"{total_comparisons} candidate comparisons exceeds n*block_k*1.2={bound}"
    )
