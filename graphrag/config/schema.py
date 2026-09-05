"""Pydantic section models mirroring `config.example.yaml` one-to-one.

Contract:
    - Every model is `frozen=True, extra="forbid"` — a typo'd key crashes startup instead of
      being silently ignored.
    - Field names match YAML keys exactly.
    - Cross-field validators that stay within one section live on that section. Validators
      that span two sections live on `Settings` (see settings.py), because a section model
      cannot see its siblings.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr, model_validator

from graphrag.core.models import BudgetLimits, ScorerWeights

_SECTION_CONFIG = ConfigDict(frozen=True, extra="forbid")


class _Section(BaseModel):
    model_config = _SECTION_CONFIG


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------
class AppSection(_Section):
    name: str
    readyz_cache_s: int
    readyz_probe_timeout_ms: int
    env: Literal["local", "staging", "prod"]
    version: str
    debug: bool
    host: str
    port: int
    workers: int
    request_timeout_s: int
    cors_origins: list[str]


# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------
class RateLimitSpec(_Section):
    query_per_minute: int
    ingest_per_minute: int
    burst_multiplier: int


class LimitsSection(_Section):
    max_query_chars: int
    max_upload_mb: int
    allowed_upload_mimetypes: list[str]
    rate_limit: RateLimitSpec
    per_request_budget: BudgetLimits


# ---------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------
class NormalizerSpec(_Section):
    unicode_form: Literal["NFC", "NFD", "NFKC", "NFKD"]
    casefold_for_hash: bool
    strip_zero_width: bool


class ParallelismSpec(_Section):
    max_concurrent_docs: int
    max_concurrent_chunks: int


class ProjectionSpec(_Section):
    queue_name: str
    max_jobs: int
    batch_size: int
    batch_linger_ms: int


class DeadLetterSpec(_Section):
    max_attempts: int
    backoff_base_s: int
    backoff_max_s: int


class IngestionSection(_Section):
    chunk_size: int
    chunk_overlap: int
    min_chunk_chars: int
    normalizer: NormalizerSpec
    parallelism: ParallelismSpec
    payload_projection: ProjectionSpec
    dead_letter: DeadLetterSpec

    @model_validator(mode="after")
    def _validate(self) -> IngestionSection:
        if not self.chunk_overlap < self.chunk_size:
            raise ValueError("ingestion.chunk_overlap must be < ingestion.chunk_size")
        if not self.min_chunk_chars < self.chunk_size:
            raise ValueError("ingestion.min_chunk_chars must be < ingestion.chunk_size")
        return self


# ---------------------------------------------------------------------------
# embedding
# ---------------------------------------------------------------------------
class DenseSpec(_Section):
    model: str
    dimensions: int
    batch_size: int
    query_prefix: str


class SparseSpec(_Section):
    model: str
    enabled: bool
    idf_modifier: bool


class EmbeddingSection(_Section):
    dense: DenseSpec
    sparse: SparseSpec

    @model_validator(mode="after")
    def _validate(self) -> EmbeddingSection:
        if self.sparse.enabled and not self.sparse.idf_modifier:
            raise ValueError(
                "embedding.sparse.idf_modifier must be true when embedding.sparse.enabled is "
                "true — omitting it degrades BM25 to raw term-frequency matching with no error"
            )
        return self


# ---------------------------------------------------------------------------
# retrieval
# ---------------------------------------------------------------------------
class VectorSpec(_Section):
    collection: str
    top_k: int
    prefetch_limit: int
    score_threshold: float | None
    timeout_ms: int


class GraphSpec(_Section):
    max_hops: int
    max_paths: int
    max_degree_per_hop: int
    hydrate_from: Literal["neo4j", "qdrant"]
    entity_link_top_k: int
    entity_link_min_score: float
    timeout_ms: int
    templates_enabled: list[str]
    text2cypher_enabled: bool

    @model_validator(mode="after")
    def _validate(self) -> GraphSpec:
        if not (1 <= self.max_hops <= 3):
            raise ValueError("retrieval.graph.max_hops must be in [1, 3]")
        return self


class FusionSpec(_Section):
    method: Literal["rrf", "dbsf"]
    rrf_k: int
    weights: dict[str, float]
    final_top_k: int


class RerankSpec(_Section):
    enabled: bool
    model: str
    top_n: int


class RetrievalSection(_Section):
    vector: VectorSpec
    graph: GraphSpec
    fusion: FusionSpec
    rerank: RerankSpec

    @model_validator(mode="after")
    def _validate(self) -> RetrievalSection:
        if not self.fusion.final_top_k <= self.vector.top_k:
            raise ValueError("retrieval.fusion.final_top_k must be <= retrieval.vector.top_k")
        return self


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------
class ResolutionSection(_Section):
    collection: str
    block_k: int
    scorer_weights: ScorerWeights
    require_type_match: bool
    auto_merge_threshold: float
    auto_reject_threshold: float
    gray_band_action: Literal["flag", "llm_adjudicate"]
    strip_suffixes: list[str]
    strip_honorifics: list[str]
    max_cluster_size: int

    @model_validator(mode="after")
    def _validate(self) -> ResolutionSection:
        if not (0 < self.auto_reject_threshold < self.auto_merge_threshold <= 1):
            raise ValueError(
                "resolution thresholds must satisfy "
                "0 < auto_reject_threshold < auto_merge_threshold <= 1"
            )
        total = (
            self.scorer_weights.jaro_winkler
            + self.scorer_weights.token_set_ratio
            + self.scorer_weights.embedding_cosine
        )
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"resolution.scorer_weights must sum to 1.0, got {total}")
        return self


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
class GraderSpec(_Section):
    batch_size: int
    fail_open: bool


class VerificationSpec(_Section):
    check_citations_exist: bool
    check_groundedness: bool
    min_groundedness_score: float
    refuse_on_failure: bool


class StreamingSpec(_Section):
    emit_node_events: bool


class OrchestrationSection(_Section):
    default_strategy: Literal["vector", "graph", "hybrid"]
    min_relevant_docs: int
    max_query_rewrites: int
    max_repair_attempts: int
    max_structured_output_repairs: int
    grader: GraderSpec
    verification: VerificationSpec
    streaming: StreamingSpec


# ---------------------------------------------------------------------------
# llm
# ---------------------------------------------------------------------------
class RoleSpec(_Section):
    model: str
    temperature: float
    max_tokens: int
    # Ceiling on ONE upstream request for this role, in seconds. Optional: unset falls back to
    # llm.request_timeout_s, so a role that names no value behaves exactly as it did before this
    # field existed. Roles differ by an order of magnitude in how long a legitimate call takes --
    # `bulk` emits up to 32k tokens per batched extraction, `router`/`grader`/`judge` return a
    # few hundred -- and one global ceiling has to be sized for the slowest of them, which leaves
    # a hung small-role call indistinguishable from a slow one for minutes.
    timeout_s: int | None = None


class BatchingSpec(_Section):
    bulk_chunks_per_request: int
    grader_docs_per_request: int


class AdaptiveRateLimitSpec(_Section):
    enabled: bool
    honor_retry_after: bool
    read_headers: list[str]
    backoff_on_429_s: int
    max_backoff_s: int


class StructuredOutputSpec(_Section):
    mode: Literal["json_schema", "json_object", "prompted"]
    repair_prompt_includes_error: bool


class LLMSection(_Section):
    gateway_base_url: str
    request_timeout_s: int
    roles: dict[str, RoleSpec]
    batching: BatchingSpec
    adaptive_rate_limit: AdaptiveRateLimitSpec
    structured_output: StructuredOutputSpec

    def timeout_for(self, role: str) -> int:
        """Seconds one upstream request for `role` may take -- its own `timeout_s` when set,
        otherwise the global `request_timeout_s`."""
        spec = self.roles.get(role)
        if spec is None or spec.timeout_s is None:
            return self.request_timeout_s
        return spec.timeout_s

    @model_validator(mode="after")
    def _validate(self) -> LLMSection:
        required = {"router", "grader", "bulk", "synth", "judge"}
        missing = required - self.roles.keys()
        if missing:
            raise ValueError(f"llm.roles is missing required role(s): {sorted(missing)}")
        return self


# ---------------------------------------------------------------------------
# stores
# ---------------------------------------------------------------------------
class HnswSpec(_Section):
    m: int
    ef_construct: int


class QdrantSpec(_Section):
    url: str
    prefer_grpc: bool
    timeout_s: int
    hnsw: HnswSpec
    sparse_modifier: Literal["idf", "none"]
    payload_indexes: list[str]


class Neo4jSpec(_Section):
    uri: str
    database: str
    store_chunk_text: bool
    max_connection_pool_size: int
    connection_timeout_s: int
    query_timeout_s: int


class PostgresSpec(_Section):
    dsn_env: str
    pool_min: int
    pool_max: int
    statement_timeout_ms: int


class RedisSpec(_Section):
    url: str
    max_connections: int


class StoresSection(_Section):
    qdrant: QdrantSpec
    neo4j: Neo4jSpec
    postgres: PostgresSpec
    redis: RedisSpec


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
class CacheTierSpec(_Section):
    enabled: bool
    ttl_s: int
    prefix: str


class LlmCacheTierSpec(_Section):
    enabled: bool
    ttl_s: int
    semantic: bool


class CacheSection(_Section):
    embedding: CacheTierSpec
    retrieval: CacheTierSpec
    llm: LlmCacheTierSpec


# ---------------------------------------------------------------------------
# resilience
# ---------------------------------------------------------------------------
class RetrySpec(_Section):
    max_attempts: int
    base_delay_ms: int
    max_delay_ms: int
    jitter: Literal["full", "equal", "none"]


class CircuitBreakerSpec(_Section):
    fail_max: int
    reset_timeout_s: int
    monitored: list[str]


class DegradationSpec(_Section):
    allow_vector_only: bool
    allow_graph_only: bool


class ResilienceSection(_Section):
    retry: RetrySpec
    circuit_breaker: CircuitBreakerSpec
    degradation: DegradationSpec


# ---------------------------------------------------------------------------
# observability
# ---------------------------------------------------------------------------
class TracesSpec(_Section):
    enabled: bool
    sample_ratio: float
    record_prompts: bool
    max_recorded_prompt_chars: int


class MetricsSpec(_Section):
    enabled: bool
    export_interval_ms: int


class LogsSpec(_Section):
    enabled: bool
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    renderer: Literal["console", "json"]
    redact_patterns: list[str]


class PhoenixSpec(_Section):
    enabled: bool
    endpoint: str
    send_full_traces: bool


class TrailSpec(_Section):
    enabled: bool
    loki_url: str
    tempo_url: str
    max_events: int
    truncate_field_chars: int


class ObservabilitySection(_Section):
    service_name: str
    otlp_endpoint: str
    otlp_protocol: Literal["grpc", "http"]
    traces: TracesSpec
    metrics: MetricsSpec
    logs: LogsSpec
    phoenix: PhoenixSpec
    trail: TrailSpec


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
class EvalMetricsSpec(_Section):
    retrieval: list[str]
    generation: list[str]
    routing: list[str]
    safety: list[str]


class EvalThresholdsSpec(_Section):
    recall_at_10: float
    faithfulness: float
    answer_relevancy: float
    routing_accuracy: float
    refusal_rate_on_unanswerable: float


class EvaluationSection(_Section):
    golden_set_path: str
    smoke_subset_size: int
    disable_cache_during_run: bool
    metrics: EvalMetricsSpec
    thresholds: EvalThresholdsSpec
    report_path: str


# ---------------------------------------------------------------------------
# security
# ---------------------------------------------------------------------------
class SecuritySection(_Section):
    api_key_header: str
    api_key_hash_algo: Literal["argon2id"]
    require_idempotency_key_on_ingest: bool
    correlation_id_header: str
    debug_endpoints_require_admin: bool
    max_request_body_mb: int


# ---------------------------------------------------------------------------
# secrets — populated ONLY from .env / Docker secrets.
# ---------------------------------------------------------------------------
class SecretsSection(_Section):
    """Populated ONLY from .env / Docker secrets. Every field is SecretStr."""

    postgres_dsn: SecretStr
    litellm_master_key: SecretStr
    litellm_virtual_key: SecretStr
    neo4j_password: SecretStr
    admin_api_key: SecretStr
