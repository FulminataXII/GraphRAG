"""Declared domain metrics. See BLUEPRINT §4.5 / ARCHITECTURE §3.5.

All instruments are declared exactly once, here, and injected wherever they're emitted.
Nothing else in the codebase may call `meter().create_counter(...)` / `create_histogram(...)`
for one of these names — that would create a second, disconnected instrument with the same
name pointed at the same metric stream, which is impossible to reason about.

Each instrument has exactly ONE emission site (`test_every_declared_metric_has_an_emitter`
enforces at most one *outside this module*, since most call sites land in later build orders —
see that test's docstring):

    route_selected          plan_route node, after the plan is resolved (incl. fail-open)
    retrieval_latency       VectorRetriever.retrieve / GraphRetriever.retrieve (@timed)
    repair_attempts         LiteLLMClient.structured, once per call with the final count
    answer_refused          insufficient node
    citations_invalid       verify_citations node
    llm_tokens              LiteLLMClient.structured
    llm_rate_limited        LiteLLMClient, on 429
    ingest_chunks_deduped   IngestionService, per chunk whose id already existed
    entities_merged         ResolutionService, per decided pair, labelled by band
    grader_degraded         grade_context node, on fail-open only
    projection_lag          ProjectionService, per batch: now - min(source.ingested_at)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Histogram, Meter

#: Names of every instrument declared below — used by the static "one emission site" scan.
INSTRUMENT_NAMES: tuple[str, ...] = (
    "route_selected",
    "retrieval_latency",
    "repair_attempts",
    "answer_refused",
    "citations_invalid",
    "llm_tokens",
    "llm_rate_limited",
    "ingest_chunks_deduped",
    "entities_merged",
    "grader_degraded",
    "projection_lag",
)


class Metrics:
    """All instruments declared once. Injected, never created ad hoc."""

    def __init__(self, meter: Meter) -> None:
        self.route_selected: Counter = meter.create_counter(
            "graphrag.route.selected", description="Route strategy selected by plan_route"
        )
        self.retrieval_latency: Histogram = meter.create_histogram(
            "graphrag.retrieval.latency", unit="ms", description="Retrieval latency by backend"
        )
        self.repair_attempts: Histogram = meter.create_histogram(
            "graphrag.repair.attempts", description="Repair attempts per structured LLM call"
        )
        self.answer_refused: Counter = meter.create_counter(
            "graphrag.answer.refused", description="Refusals by reason"
        )
        self.citations_invalid: Counter = meter.create_counter(
            "graphrag.citations.invalid",
            description="Fabricated citations caught deterministically",
        )
        self.llm_tokens: Counter = meter.create_counter(
            "graphrag.llm.tokens", description="LLM tokens by model, role, direction"
        )
        self.llm_rate_limited: Counter = meter.create_counter(
            "graphrag.llm.rate_limited", description="429s by provider"
        )
        self.ingest_chunks_deduped: Counter = meter.create_counter(
            "graphrag.ingest.chunks.deduped", description="Chunks deduped on ingest"
        )
        self.entities_merged: Counter = meter.create_counter(
            "graphrag.entities.merged", description="Entity merge decisions by band"
        )
        self.grader_degraded: Counter = meter.create_counter(
            "graphrag.grader.degraded", description="grade_context fail-open events"
        )
        self.projection_lag: Histogram = meter.create_histogram(
            "graphrag.projection.lag", unit="ms", description="Projection lag per batch"
        )
