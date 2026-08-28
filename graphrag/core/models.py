"""Domain entities. See BLUEPRINT §3.2 and the Type Index (§1a).

All frozen pydantic models. `BudgetLimits` and `ScorerWeights` live here (not in
`config/schema.py`) because both a config section and a service need them, and two parallel
declarations would drift — `config/schema.py` imports and reuses these.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator


class DocumentStatus(StrEnum):
    PENDING = "PENDING"
    PARSING = "PARSING"
    EMBEDDING = "EMBEDDING"
    EXTRACTING = "EXTRACTING"
    RESOLVING = "RESOLVING"
    INDEXED = "INDEXED"
    FAILED = "FAILED"
    DELETING = "DELETING"


class EntityType(StrEnum):
    PERSON = "PERSON"
    ORG = "ORG"
    LOCATION = "LOCATION"
    PRODUCT = "PRODUCT"
    EVENT = "EVENT"
    CONCEPT = "CONCEPT"


class SparseVector(BaseModel):
    """A sparse embedding: parallel index/value arrays, as Qdrant expects.

    Contract:
        - len(indices) == len(values); indices are unique and ascending.
        - An EMPTY sparse vector (both arrays empty) is legal and must not raise anywhere.
          BM25 legitimately returns no terms for a query of pure stopwords.
        - Values are raw term frequencies. IDF is applied server-side by Qdrant via
          Modifier.IDF.
    """

    model_config = ConfigDict(frozen=True)

    indices: list[int]
    values: list[float]

    @model_validator(mode="after")
    def _validate(self) -> SparseVector:
        if len(self.indices) != len(self.values):
            raise ValueError("SparseVector.indices and .values must have equal length")
        if self.indices != sorted(set(self.indices)):
            raise ValueError("SparseVector.indices must be unique and ascending")
        return self


class BudgetLimits(BaseModel):
    """Per-request CEILINGS. The counterpart to Spend, which holds consumption.

    Lives in core (not config) because both LimitsSection and the orchestrator need it, and
    two parallel declarations would drift. `remaining = limits - spent`, computed on read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_llm_calls: int
    max_wall_ms: int
    max_prompt_tokens: int


class ScorerWeights(BaseModel):
    """Entity-resolution scorer weights. Must sum to 1.0 (validated in config).

    In core for the same reason as BudgetLimits: ResolutionSection and score_pair both use it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    jaro_winkler: float
    token_set_ratio: float
    embedding_cosine: float


class JobStatus(BaseModel):
    """Queue-level job state, returned by JobQueue.status().

    Distinct from DocumentStatus: this is about the JOB (queued/running/failed/complete),
    DocumentStatus is about the DOCUMENT's position in the pipeline. A job can be `complete`
    while its document is `FAILED`.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    state: Literal["deferred", "queued", "in_progress", "complete", "failed", "not_found"]
    attempts: int
    enqueued_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    error: str | None


class StructuredResult[T](BaseModel):
    """Return type of LLMClient.structured().

    Defined HERE, in core, not beside the LiteLLM adapter — core/ports.py references it, and
    core may not import adapters. The adapter constructs it; the port declares it.
    """

    model_config = ConfigDict(frozen=True)

    value: T
    model_served: str
    repair_attempts: int
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


class SourceRef(BaseModel):
    """One document's claim on a chunk.

    Identity is `doc_id` alone — it must match the Postgres PK (chunk_id, doc_id).
    Consequence: if the SAME document repeats an identical paragraph at two offsets, that is
    ONE source row, recording the FIRST occurrence's offsets. Accepted deliberately: the
    alternative (PK including char_start) multiplies rows for boilerplate like headers and
    disclaimers while adding nothing a citation can use.
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    uri: str
    page: int | None
    char_start: int
    char_end: int
    ingested_at: AwareDatetime


class Chunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: UUID
    text: str
    content_hash: str
    sources: list[SourceRef]
    entity_ids: list[UUID]
    schema_version: int = 1


class ScoredChunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk: Chunk
    score: float
    rank: int
    origin: Literal["vector", "graph", "fused"]


class Mention(BaseModel):
    model_config = ConfigDict(frozen=True)

    surface: str
    type: EntityType
    chunk_id: UUID
    char_start: int
    char_end: int
    confidence: float


class Entity(BaseModel):
    model_config = ConfigDict(frozen=True)

    canonical_id: UUID
    name: str
    name_normalized: str
    type: EntityType
    aliases: list[str]
    mention_count: int


class Relation(BaseModel):
    """A typed edge. Provenance fields are REQUIRED — a relation without chunk_id is invalid."""

    model_config = ConfigDict(frozen=True)

    src_id: UUID
    dst_id: UUID
    type: str
    confidence: float
    chunk_id: UUID
    doc_id: str
    evidence_span: str


class GraphPath(BaseModel):
    model_config = ConfigDict(frozen=True)

    nodes: list[Entity]
    relations: list[Relation]
    chunk_ids: list[UUID]
    hops: int
    score: float


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: UUID
    doc_id: str
    uri: str
    quote: str | None


class Answer(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    citations: list[Citation]
    confidence: float


class Spend(BaseModel):
    """Resources ALREADY CONSUMED. Never 'remaining'.

    Contract:
        - merge(a, b) is associative and commutative.
        - llm_calls and tokens aggregate with +; wall_ms aggregates with max (branches
          overlap).
    """

    model_config = ConfigDict(frozen=True)

    llm_calls: int = 0
    tokens: int = 0
    wall_ms: int = 0

    @staticmethod
    def merge(a: Spend, b: Spend) -> Spend:
        return Spend(
            llm_calls=a.llm_calls + b.llm_calls,
            tokens=a.tokens + b.tokens,
            wall_ms=max(a.wall_ms, b.wall_ms),
        )

    def exceeds(self, limits: BudgetLimits) -> str | None:
        """Return the name of the first breached limit, or None."""
        if self.llm_calls > limits.max_llm_calls:
            return "max_llm_calls"
        if self.wall_ms > limits.max_wall_ms:
            return "max_wall_ms"
        if self.tokens > limits.max_prompt_tokens:
            return "max_prompt_tokens"
        return None


class NodeFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    node: str
    code: str
    message: str
    attempt: int
    at: AwareDatetime


class RoutePlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy: Literal["vector", "graph", "hybrid"]
    seed_entities: list[str]
    hops: int
    sub_queries: list[str]
    rationale: str


class DocumentRecord(BaseModel):
    """Ledger row.

    doc_id derivation: uuid.UUID(bytes=sha256(file_bytes).digest()[:16], version=5), same
    construction as chunk_id. Content-addressed, so re-uploading a byte-identical file yields
    the same doc_id and `register()` returns False — the API then returns 200 with the
    existing doc_id rather than 202 with a new job. The client's Idempotency-Key guards
    against duplicate *requests*; the sha256 guards against duplicate *content*. Both are
    needed: the same bytes can arrive under two different keys.
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    uri: str
    sha256: str
    mime_type: str
    status: DocumentStatus
    error_code: str | None
    attempts: int
    corpus_version: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
