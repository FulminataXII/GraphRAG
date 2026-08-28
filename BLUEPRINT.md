# BLUEPRINT — Hybrid GraphRAG

Implementation contract for `ARCHITECTURE.md`. This document says **what** each component is and
what it guarantees. It does not argue **why** — that's the architecture doc.

**How to use this:** find your component, implement exactly its signature and contract, write its
tests, move on. If you need to know how another component behaves, read its contract here — do not
read its implementation. If a contract is ambiguous, stop and flag it rather than guessing.

---

## 0. Global Conventions

**Package root:** `graphrag/`. All imports absolute (`from graphrag.core.models import Chunk`).

**Layering — enforced in CI, no exceptions:**

| Layer | May import from | Must NOT import |
|---|---|---|
| `core/` | stdlib, pydantic **only** | any `graphrag.*` — this is the leaf |
| `config/` | stdlib, pydantic, `core` | `services`, `adapters`, `apps` |
| `services/` | `core`, `config` | `adapters`, `apps`, any I/O library |
| `adapters/` | `core`, `config`, I/O libs | `services`, `apps` |
| `apps/` | everything | — |

`config/` may import `core` because shared value types (`BudgetLimits`, `ScorerWeights`) are
domain concepts that both a config section and a service need. They live in `core/models.py`;
`config/schema.py` reuses them rather than redeclaring a parallel set that can drift.

`services/` may import `config/` **for types only** — a section model as a constructor parameter
is fine. Calling `get_settings()` inside a service is not; see §5.2.

`services/` receives capabilities through `core.ports` Protocols passed to constructors. A service
that imports `qdrant_client` is a bug, not a shortcut.

**Async:** every function that performs I/O is `async def`. Pure functions are sync. No `asyncio.run`
outside `apps/`.

**Typing:** `mypy --strict` passes on `core/`, `services/`, `config/`. `from __future__ import
annotations` at the top of every module. No bare `Any` outside adapter boundaries.

**Errors:** every module raises only `graphrag.core.errors.AppError` subclasses or lets library
exceptions escape to the nearest adapter, which wraps them. Services never raise library exceptions.

**Time & IDs:** never call `datetime.now()` or `uuid4()` directly in `core/` or `services/`. Use the
injected `Clock` and `IdGenerator` ports. This is what makes time-dependent tests deterministic.

**Docstring contract format** — every public component uses this shape:

```python
"""One-line summary.

Contract:
    - Guarantee 1 (what callers may rely on)
    - Guarantee 2
Raises:
    SomeError: when ...
"""
```

**Naming:** `*Port` = Protocol, `*Adapter`/`*Store`/`*Client` = implementation, `*Service` = use case,
`*Request`/`*Response` = HTTP DTO, `*Envelope` = queue payload.

**Python 3.12 conventions — apply these throughout:**

- **PEP 695 generics.** Write `class JobEnvelope[P](BaseModel)` and `def f[T](x: T) -> T`, not
  `Generic[P]` / `TypeVar`. Pydantic v2 supports PEP 695 type parameters.
- **All datetimes are timezone-aware UTC.** `datetime.now(UTC)`, never `utcnow()` (deprecated
  since 3.12 and returns a naive object, which silently breaks comparisons). Every `datetime`
  field is annotated `AwareDatetime`, so pydantic rejects naive values at the boundary.
- **`X | None`, not `Optional[X]`.** `list[X]`, not `List[X]`.
- **`StrEnum`** for string enums (3.11+), so `str(x) == x.value` and JSON serialization is free.
- **`@runtime_checkable` caveat.** `isinstance()` against a Protocol only checks that the
  attribute *names* exist — it does not check signatures, so `test_fakes_satisfy_protocols`
  catches a missing method but not a wrong one. Real conformance is enforced by `mypy --strict`
  in CI, where each fake is assigned to its port type. Treat the isinstance test as a smoke
  check, not the guarantee.
- **`asyncio.TaskGroup`** for concurrent fan-out (3.11+), not bare `gather` — it cancels
  siblings on first failure instead of leaking orphan tasks.
- **No mutable default arguments**, and no module-level mutable state anywhere in `core/` or
  `services/`.

---

## 1. Repository Layout

```
graphrag-hybrid/
├── pyproject.toml                  uv project; deps, ruff, mypy, pytest config
├── docker-compose.yml              core profile
├── docker-compose.obs.yml          observability profile
├── docker-compose.prod.yml         prod overlay
├── Dockerfile                      multi-stage, one image, three entrypoints
├── Makefile                        up/down/test/eval/lint/trail
├── .env.example                    secrets template (committed)
├── otel/collector.yaml             OTel Collector: dual export, NO span filter
├── litellm/config.yaml             model_list, aliases, fallbacks, rpm/tpm, otel callback
├── scripts/check_layering.py       CI guard for the import table above
├── corpus/                         seed documents (you supply — see MANUAL)
│
├── graphrag/
│   ├── config/
│   │   ├── schema.py               section models (AppSection, RetrievalSection, ...)
│   │   ├── settings.py             Settings, get_settings(), source precedence
│   │   ├── validate.py             `python -m graphrag.config.validate` entrypoint
│   │   ├── base.yaml  local.yaml  prod.yaml
│   ├── core/
│   │   ├── ids.py                  content addressing, correlation IDs
│   │   ├── models.py               domain entities
│   │   ├── events.py               queue payload schemas (versioned)
│   │   ├── errors.py               AppError hierarchy + error codes
│   │   └── ports.py                Protocols
│   ├── services/
│   │   ├── ingestion/              parser, normalizer, chunker, service
│   │   ├── resolution/             normalize, blocking, scoring, clustering, service
│   │   ├── retrieval/              vector, graph, fusion, linker, service
│   │   └── orchestration/          state, schemas, prompts, nodes/, graph
│   ├── adapters/
│   │   ├── telemetry/              otel, logging, decorators, middleware, metrics, trail
│   │   ├── fastembed_embedder.py
│   │   ├── qdrant_store.py
│   │   ├── neo4j_store.py
│   │   ├── litellm_client.py
│   │   ├── redis_cache.py
│   │   ├── arq_queue.py
│   │   └── postgres/               tables, ledger, sources, evals, migrations/
│   ├── apps/
│   │   ├── api/                    main, deps, errors, schemas, routers/
│   │   ├── worker/                 settings, tasks/
│   │   └── cli/                    main.py (typer)
│   └── evaluation/
│       ├── golden/                 *.yaml fixtures (you supply)
│       ├── metrics/                retrieval.py, generation.py, routing.py
│       ├── runner.py  report.py
└── tests/
    ├── conftest.py  fakes.py  factories.py
    ├── unit/  integration/  contract/  eval/
```

---

## 1a. Type Index — where every named type lives

**Before referencing a type, look it up here.** If a type you need isn't listed, that's a spec
gap: stop and report it rather than inventing one. Types are declared in exactly one module.

### `core/models.py` — BO-01 (importable by everything)
`SparseVector` · `BudgetLimits` · `ScorerWeights` · `JobStatus` · `StructuredResult[T]` ·
`SourceRef` · `Chunk` · `ScoredChunk` · `Mention` · `Entity` · `Relation` · `GraphPath` ·
`Citation` · `Answer` · `Spend` · `NodeFailure` · `RoutePlan` · `DocumentRecord` ·
`DocumentStatus` · `EntityType`

### `core/errors.py` — BO-01
`AppError` and its subclasses (§3.3).

### `core/events.py` — BO-01
`JobEnvelope[P]` · `IngestDocumentPayload` · `ExtractEntitiesPayload` · `ProjectPayloadPayload` ·
`DeleteDocumentPayload`

### `config/schema.py` — BO-00
All `*Section` models, plus the nested spec models they contain: `RateLimitSpec`,
`NormalizerSpec`, `ParallelismSpec`, `ProjectionSpec`, `DeadLetterSpec`, `DenseSpec`,
`SparseSpec`, `VectorSpec`, `GraphSpec`, `FusionSpec`, `RerankSpec`. **Each mirrors the
correspondingly-named mapping in `config.example.yaml` field for field** — that file is the
authoritative shape; these are its typed counterpart. `BudgetLimits` and `ScorerWeights` are
imported from `core`, not redeclared.

### Declared in the BO that builds them
| Type | Module | BO |
|---|---|---|
| `TrailEvent`, `TrailBundle` | `adapters/telemetry/trail.py` | 02 |
| `ErrorBody`, `ErrorEnvelope` | `apps/api/errors.py` | 03 |
| `ParsedDocument`, `ChunkSpec` | `services/ingestion/` | 05 |
| `MentionOut`, `RelationOut`, `CitationOut`, `EntityExtraction`, `RoutePlanOut`, `RelevanceGrade`, `RelevanceGradeBatch`, `RewrittenQuery`, `AnswerOut`, `Entailment` | `services/orchestration/schemas.py` | 06 |
| `ResolutionResult` | `services/resolution/service.py` | 07 |
| `QueryState`, `NodeDeps`, `QueryResult`, `StreamEvent` | `services/orchestration/` | 10 |
| `GoldenItem`, `ConfusionMatrix`, `EvalReport` | `evaluation/` | 11 |
| `ApiKey` | `apps/api/deps.py` | 12 |

**The `*Out` suffix marks an LLM output schema** — what the model is asked to produce. It is
validated and then mapped to the corresponding domain model in `core/models.py`. `AnswerOut` is
not `Answer`; the former is untrusted until validation succeeds.

---

## 2. `graphrag/config/`

### 2.1 `config/schema.py`

Pydantic `BaseModel` sections mirroring `config.example.yaml` one-to-one. All are `frozen=True,
extra="forbid"`. Field names match YAML keys exactly.

```python
class AppSection(BaseModel):
    name: str; env: Literal["local","staging","prod"]; version: str
    debug: bool; host: str; port: int; workers: int
    request_timeout_s: int; cors_origins: list[str]

class LimitsSection(BaseModel):
    max_query_chars: int; max_upload_mb: int
    allowed_upload_mimetypes: list[str]
    rate_limit: RateLimitSpec
    per_request_budget: BudgetLimits          # LIMITS ONLY — see core/models.Spend

class IngestionSection(BaseModel):
    chunk_size: int; chunk_overlap: int; min_chunk_chars: int
    normalizer: NormalizerSpec
    parallelism: ParallelismSpec
    payload_projection: ProjectionSpec        # queue_name, max_jobs, batch_size, batch_linger_ms
    dead_letter: DeadLetterSpec

class EmbeddingSection(BaseModel):
    dense: DenseSpec                          # model, dimensions, batch_size, query_prefix
    sparse: SparseSpec                        # model, enabled, idf_modifier

class RetrievalSection(BaseModel):
    vector: VectorSpec; graph: GraphSpec; fusion: FusionSpec; rerank: RerankSpec

class ResolutionSection(BaseModel):
    collection: str; block_k: int
    scorer_weights: ScorerWeights
    require_type_match: bool
    auto_merge_threshold: float; auto_reject_threshold: float
    gray_band_action: Literal["flag","llm_adjudicate"]
    strip_suffixes: list[str]; strip_honorifics: list[str]
    max_cluster_size: int

class OrchestrationSection(BaseModel): ...    # see config.example.yaml
class LLMSection(BaseModel): ...              # roles, batching, adaptive_rate_limit
class StoresSection(BaseModel): ...           # qdrant, neo4j, postgres, redis
class CacheSection(BaseModel): ...
class ResilienceSection(BaseModel): ...
class ObservabilitySection(BaseModel): ...
class EvaluationSection(BaseModel): ...
class SecuritySection(BaseModel): ...

class SecretsSection(BaseModel):
    """Populated ONLY from .env / Docker secrets. Every field is SecretStr."""
    postgres_dsn: SecretStr
    litellm_master_key: SecretStr
    litellm_virtual_key: SecretStr
    neo4j_password: SecretStr
    admin_api_key: SecretStr
```

**Cross-field validators live on the sections that own them**, not on `Settings`:
- `ResolutionSection`: `0 < auto_reject < auto_merge <= 1`; `sum(scorer_weights) == 1.0 ± 1e-9`
- `IngestionSection`: `chunk_overlap < chunk_size`; `min_chunk_chars < chunk_size`
- `RetrievalSection`: `1 <= graph.max_hops <= 3`; `fusion.final_top_k <= vector.top_k`
- `EmbeddingSection`: `sparse.enabled` implies `sparse.idf_modifier is True`

### 2.2 `config/settings.py`

```python
class LayeredYamlSource(PydanticBaseSettingsSource):
    """Deep-merging multi-file YAML source. Do NOT use the stock YamlConfigSettingsSource here.

    Why this exists:
        pydantic-settings merges a list of config files SHALLOWLY. Given
        `yaml_file=["base.yaml", "local.yaml"]`, a `local.yaml` containing only

            observability:
              logs:
                level: DEBUG

        REPLACES the whole `observability` mapping rather than overriding one leaf. Every other
        key in that section vanishes, and because those fields are required the process fails at
        startup with a confusing "field required" error that points at base.yaml, which is
        correct. Partial environment overrides — the entire point of base/local/prod layering —
        do not work with the stock source.

    Contract:
        - Reads each path in order; a missing path is skipped silently (prod.yaml need not exist
          in dev), but a malformed one raises.
        - Deep-merges dict values recursively; later files win.
        - Scalars and LISTS are replaced wholesale, never concatenated. `cors_origins: [a]` in
          local.yaml replaces base's list; it does not append. This is the least surprising rule
          and matches every mainstream config merger.
        - Returns the merged mapping. Validation is pydantic's job, not this source's.
        - Pure and side-effect free: same files in, same dict out.
    """
    def __init__(self, settings_cls: type[BaseSettings], paths: Sequence[str]) -> None: ...
    def __call__(self) -> dict[str, Any]: ...

    # Also owns YAML typo protection: after merging, every top-level key must appear in
    # settings_cls.model_fields, else raise ValueError naming the offender. This lives here
    # because Settings itself must use extra="ignore" — see the warning below.

def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursive dict merge. Overlay wins. Lists and scalars replace, never merge."""
```

```python
class Settings(BaseSettings):
    """Single source of truth for all configuration.

    Contract:
        - Constructed exactly once per process, via get_settings().
        - Immutable (frozen). Never model_copy() it — cached_property would carry a stale hash.
        - Unknown keys in YAML raise ValueError at construction — enforced by LayeredYamlSource,
          not by extra="forbid". See the warning below.
        - Precedence, highest first: init > env > .env > YAML > field defaults.
        - config_hash covers everything EXCEPT `secrets`; it is safe to log and to attach
          to telemetry.
    """
    model_config = SettingsConfigDict(
        env_prefix="GRAPHRAG_", env_nested_delimiter="__", env_file=".env",
        # No yaml_file here — LayeredYamlSource owns the paths. Setting both would be two
        # sources of truth for which files load, and the stock one merges shallowly.
        # extra="ignore", NOT "forbid" — see the warning below. Typo protection for YAML
        # lives in LayeredYamlSource instead.
        frozen=True, extra="ignore",
    )
    app: AppSection
    limits: LimitsSection
    ingestion: IngestionSection
    embedding: EmbeddingSection
    retrieval: RetrievalSection
    resolution: ResolutionSection
    orchestration: OrchestrationSection
    llm: LLMSection
    stores: StoresSection
    cache: CacheSection
    resilience: ResilienceSection
    observability: ObservabilitySection
    evaluation: EvaluationSection
    security: SecuritySection
    secrets: SecretsSection

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings
                                   ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order is highest-priority-first. Note the CUSTOM yaml source — see below."""
        return (init_settings, env_settings, dotenv_settings,
                LayeredYamlSource(settings_cls,
                                  paths=["config/base.yaml",
                                         f"config/{os.getenv('APP_ENV','local')}.yaml"]),
                file_secret_settings)

    @model_validator(mode="after")
    def _cross_section(self) -> Settings:
        """Validate invariants that span two sections.

        Rules:
            - llm.roles['judge'].model != llm.roles['synth'].model
            - resilience.degradation.allow_graph_only implies stores.neo4j.store_chunk_text
            - observability.trail.enabled must be False when app.env == 'prod'
            - security.debug_endpoints_require_admin must be True when app.env == 'prod'

        NOT checked here: that embedding.dense.dimensions matches the real model's output.
        Confirming that requires loading the model — I/O in a pure config object. It is
        asserted in Container.create() instead, before ensure_collections(), where a mismatch
        raises rather than silently creating a collection of the wrong width.
        """

    @computed_field  # type: ignore[misc]
    @cached_property
    def config_hash(self) -> str:
        """sha256 of model_dump(mode='json', exclude={'secrets'}), keys sorted. 64 hex chars."""

def get_settings() -> Settings:
    """Process-wide singleton. lru_cache(maxsize=1). The ONLY construction site for Settings."""
```

### 2.3 `config/validate.py`

```python
def main() -> int:
    """Entrypoint: `python -m graphrag.config.validate`.

    Contract:
        - Exit 0 and print the config_hash on success.
        - Exit 1 and print the full Pydantic error tree on failure.
        - Never prints a secret value.
    Used as the compose init gate before api/worker start.
    """
```

---

## 3. `graphrag/core/` — pure domain, zero I/O

### 3.1 `core/ids.py`

```python
CHUNK_ID_VERSION: Final[int] = 5

def normalize_for_hash(text: str, *, unicode_form: str = "NFKC",
                       casefold: bool = True, strip_zero_width: bool = True) -> str:
    """Canonical form used ONLY for hashing. Never stored, never displayed.

    Contract:
        - Idempotent: f(f(x)) == f(x) for all x.
        - Deterministic across processes and platforms.
        - Order: strip zero-width -> unicode normalize -> collapse all whitespace runs to a
          single U+0020 -> strip -> casefold (if enabled).
    """

def content_hash(text: str) -> str:
    """sha256 hex of normalize_for_hash(text). 64 chars. Stored as payload.content_hash."""

def chunk_id(text: str) -> UUID:
    """Deterministic content-addressed chunk identifier.

    Contract:
        - Returns uuid.UUID(bytes=sha256(normalize_for_hash(text)).digest()[:16], version=5)
        - MUST pass version=5. Omitting it leaves the RFC 4122 version nibble as an accidental
          hash artefact.
        - Identical normalized text ALWAYS yields the same UUID; this is the sole dedup mechanism.
        - Do not substitute a 64-bit integer ID.
    """

def entity_id(canonical_name: str, entity_type: str) -> UUID:
    """Deterministic entity ID from normalized name + type. Same construction as chunk_id."""

def new_correlation_id() -> str:
    """26-char Crockford base32 ULID. Sortable by creation time."""
```

### 3.2 `core/models.py`

All frozen pydantic models.

```python
class SparseVector(BaseModel):
    """A sparse embedding: parallel index/value arrays, as Qdrant expects.

    Contract:
        - len(indices) == len(values); indices are unique and ascending.
        - An EMPTY sparse vector (both arrays empty) is legal and must not raise anywhere.
          BM25 legitimately returns no terms for a query of pure stopwords.
        - Values are raw term frequencies. IDF is applied server-side by Qdrant via Modifier.IDF.
    """
    indices: list[int]; values: list[float]

class BudgetLimits(BaseModel):
    """Per-request CEILINGS. The counterpart to Spend, which holds consumption.

    Lives in core (not config) because both LimitsSection and the orchestrator need it, and two
    parallel declarations would drift. `remaining = limits - spent`, computed on read.
    """
    max_llm_calls: int; max_wall_ms: int; max_prompt_tokens: int

class ScorerWeights(BaseModel):
    """Entity-resolution scorer weights. Must sum to 1.0 (validated in config).

    In core for the same reason as BudgetLimits: ResolutionSection and score_pair both use it.
    """
    jaro_winkler: float; token_set_ratio: float; embedding_cosine: float

class JobStatus(BaseModel):
    """Queue-level job state, returned by JobQueue.status().

    Distinct from DocumentStatus: this is about the JOB (queued/running/failed/complete),
    DocumentStatus is about the DOCUMENT's position in the pipeline. A job can be `complete`
    while its document is `FAILED`.
    """
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
    doc_id: str; uri: str; page: int | None
    char_start: int; char_end: int; ingested_at: datetime

class Chunk(BaseModel):
    chunk_id: UUID; text: str; content_hash: str
    sources: list[SourceRef]; entity_ids: list[UUID]
    schema_version: int = 1

class ScoredChunk(BaseModel):
    chunk: Chunk; score: float; rank: int
    origin: Literal["vector", "graph", "fused"]

class Mention(BaseModel):
    surface: str; type: EntityType
    chunk_id: UUID; char_start: int; char_end: int; confidence: float

class Entity(BaseModel):
    canonical_id: UUID; name: str; name_normalized: str
    type: EntityType; aliases: list[str]; mention_count: int

class Relation(BaseModel):
    """A typed edge. Provenance fields are REQUIRED — a relation without chunk_id is invalid."""
    src_id: UUID; dst_id: UUID; type: str; confidence: float
    chunk_id: UUID; doc_id: str; evidence_span: str

class GraphPath(BaseModel):
    nodes: list[Entity]; relations: list[Relation]
    chunk_ids: list[UUID]; hops: int; score: float

class Citation(BaseModel):
    chunk_id: UUID; doc_id: str; uri: str; quote: str | None

class Answer(BaseModel):
    text: str; citations: list[Citation]; confidence: float

class Spend(BaseModel):
    """Resources ALREADY CONSUMED. Never 'remaining'.

    Contract:
        - merge(a, b) is associative and commutative.
        - llm_calls and tokens aggregate with +; wall_ms aggregates with max (branches overlap).
    """
    llm_calls: int = 0; tokens: int = 0; wall_ms: int = 0

    @staticmethod
    def merge(a: Spend, b: Spend) -> Spend: ...
    def exceeds(self, limits: BudgetLimits) -> str | None:
        """Return the name of the first breached limit, or None."""

class NodeFailure(BaseModel):
    node: str; code: str; message: str; attempt: int; at: datetime

class RoutePlan(BaseModel):
    strategy: Literal["vector", "graph", "hybrid"]
    seed_entities: list[str]; hops: int
    sub_queries: list[str]; rationale: str

class DocumentRecord(BaseModel):
    """Ledger row.

    doc_id derivation: uuid.UUID(bytes=sha256(file_bytes).digest()[:16], version=5), same
    construction as chunk_id. Content-addressed, so re-uploading a byte-identical file yields
    the same doc_id and `register()` returns False — the API then returns 200 with the existing
    doc_id rather than 202 with a new job. The client's Idempotency-Key guards against duplicate
    *requests*; the sha256 guards against duplicate *content*. Both are needed: the same bytes
    can arrive under two different keys.
    """
    doc_id: str; uri: str; sha256: str; mime_type: str
    status: DocumentStatus; error_code: str | None; attempts: int
    corpus_version: int; created_at: AwareDatetime; updated_at: AwareDatetime

class DocumentStatus(StrEnum):
    PENDING = "PENDING"; PARSING = "PARSING"; EMBEDDING = "EMBEDDING"
    EXTRACTING = "EXTRACTING"; RESOLVING = "RESOLVING"
    INDEXED = "INDEXED"; FAILED = "FAILED"; DELETING = "DELETING"

class EntityType(StrEnum):
    PERSON = "PERSON"; ORG = "ORG"; LOCATION = "LOCATION"
    PRODUCT = "PRODUCT"; EVENT = "EVENT"; CONCEPT = "CONCEPT"
```

### 3.3 `core/errors.py`

```python
class AppError(Exception):
    """Base. Every error crossing a service boundary is one of these.

    Contract:
        - `code` is stable and machine-readable; it appears verbatim in API responses.
        - `http_status` and `retryable` are class attributes, not instance state.
        - `details` must never contain secrets or raw prompts.
    """
    code: ClassVar[str]; http_status: ClassVar[int]; retryable: ClassVar[bool]
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None: ...

# code                            http  retryable
class ValidationError(AppError)         # VALIDATION_ERROR              422  False
class AuthInvalidKey(AppError)          # AUTH_INVALID_KEY              401  False
class RateLimited(AppError)             # RATE_LIMITED                  429  True   (+retry_after)
class LLMSchemaViolation(AppError)      # LLM_SCHEMA_VIOLATION          502  True
class LLMProviderExhausted(AppError)    # LLM_PROVIDER_EXHAUSTED        503  True
class RetrievalBackendUnavailable(AppError)  # RETRIEVAL_BACKEND_UNAVAILABLE 503 True
class GraphBackendUnavailable(AppError) # GRAPH_BACKEND_UNAVAILABLE     503  True
class BudgetExceeded(AppError)          # BUDGET_EXCEEDED               200  False  (-> refusal)
class NotFound(AppError)                # NOT_FOUND                     404  False
class ConflictError(AppError)           # CONFLICT                      409  False
class InternalError(AppError)           # INTERNAL_ERROR                500  False
```

### 3.4 `core/events.py`

```python
SCHEMA_VERSION: Final[int] = 1

class JobEnvelope(BaseModel, Generic[P]):
    """Every queue message. Passed as a positional job ARGUMENT, never via arq's ctx.

    Contract:
        - `otel` carries the W3C traceparent injected at enqueue time.
        - Workers reject an envelope whose schema_version major differs from SCHEMA_VERSION.
        - Must be JSON-serializable end to end.
    """
    schema_version: int = SCHEMA_VERSION
    correlation_id: str
    otel: dict[str, str]
    enqueued_at: datetime
    payload: P

class IngestDocumentPayload(BaseModel):
    doc_id: str; uri: str; sha256: str; mime_type: str

class ExtractEntitiesPayload(BaseModel):
    doc_id: str; chunk_ids: list[UUID]

class ProjectPayloadPayload(BaseModel):
    """Batch of chunk_ids whose Qdrant payload must be re-derived from Postgres."""
    chunk_ids: list[UUID]

class DeleteDocumentPayload(BaseModel):
    doc_id: str
```

### 3.5 `core/ports.py`

Protocols only. `@runtime_checkable`. No implementations, no imports of I/O libraries.

```python
class Clock(Protocol):
    def now(self) -> datetime: ...

class IdGenerator(Protocol):
    def new_correlation_id(self) -> str: ...
    def new_job_id(self) -> str: ...

class Embedder(Protocol):
    async def embed_dense(self, texts: Sequence[str], *, is_query: bool = False
                          ) -> list[list[float]]: ...
    async def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]: ...
    @property
    def dimensions(self) -> int: ...

class VectorStore(Protocol):
    async def ensure_collections(self) -> None:
        """Idempotent. MUST create the sparse vector config with the IDF modifier."""
    async def upsert_chunks(self, chunks: Sequence[Chunk],
                            dense: Sequence[list[float]], sparse: Sequence[SparseVector]) -> None: ...
    async def set_sources(self, chunk_id: UUID, sources: list[SourceRef]) -> None:
        """Overwrite the sources array wholesale. Caller supplies the authoritative list."""
    async def delete_chunks(self, chunk_ids: Sequence[UUID]) -> None: ...
    async def get_chunks(self, chunk_ids: Sequence[UUID]) -> list[Chunk]: ...
    async def hybrid_search(self, *, dense: list[float], sparse: SparseVector,
                            top_k: int, prefetch_limit: int, rrf_k: int,
                            weights: dict[str, float] | None,
                            filters: dict[str, Any] | None = None) -> list[ScoredChunk]: ...
    async def upsert_entities(self, entities: Sequence[Entity],
                              vectors: Sequence[list[float]]) -> None: ...
    async def search_entities(self, vector: list[float], *, top_k: int,
                              entity_type: EntityType | None) -> list[tuple[Entity, float]]: ...
    async def health(self) -> bool: ...

class GraphStore(Protocol):
    async def ensure_schema(self) -> None:
        """Idempotent: constraints + indexes from ARCHITECTURE §6.3."""
    async def upsert_document(self, doc_id: str, uri: str, title: str, sha256: str) -> None: ...
    async def upsert_chunks(self, doc_id: str, chunks: Sequence[Chunk]) -> None:
        """Stores FULL chunk text (Chunk.text), not a preview."""
    async def upsert_entities(self, entities: Sequence[Entity]) -> None: ...
    async def upsert_relations(self, relations: Sequence[Relation]) -> None:
        """Rejects any relation with a null chunk_id or doc_id."""
    async def add_alias(self, alias_id: UUID, canonical_id: UUID,
                        score: float, method: str) -> None: ...
    async def traverse(self, template: str, params: dict[str, Any],
                       *, timeout_ms: int) -> list[GraphPath]: ...
    async def get_chunks(self, chunk_ids: Sequence[UUID]) -> list[Chunk]:
        """Hydrate full chunk text from the graph.

        Contract:
            - Returns Chunk objects with `text` AND `sources` populated. Sources are
              reconstructed from the (:Document)-[:HAS_CHUNK]->(:Chunk) edges, which is why
              HAS_CHUNK carries the offsets.
            - Returning text without sources would make every graph-path answer uncitable and
              would silently fail `verify_citations`, so this is not optional.
            - This is both the graph retrieval hydration path AND the vector-outage fallback.
        """
    async def delete_document(self, doc_id: str) -> None: ...
    async def health(self) -> bool: ...

class LLMClient(Protocol):
    async def structured(self, *, role: str, messages: list[dict[str, str]],
                         schema: type[T], max_repairs: int) -> StructuredResult[T]: ...
    async def stream_text(self, *, role: str,
                          messages: list[dict[str, str]]) -> AsyncIterator[str]: ...
    async def health(self) -> bool: ...

class Cache(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, *, ttl_s: int) -> None: ...
    async def delete_prefix(self, prefix: str) -> int: ...

class JobQueue(Protocol):
    async def enqueue(self, task: str, envelope: JobEnvelope[Any], *,
                      job_id: str | None = None, queue_name: str | None = None) -> str: ...
    async def status(self, job_id: str) -> JobStatus: ...

class DocumentLedger(Protocol):
    async def register(self, doc_id: str, uri: str, sha256: str, mime_type: str) -> bool:
        """Returns False if sha256 already present (duplicate upload)."""
    async def set_status(self, doc_id: str, status: DocumentStatus,
                         error_code: str | None = None) -> None: ...
    async def get(self, doc_id: str) -> DocumentRecord | None: ...
    async def bump_corpus_version(self) -> int: ...
    async def current_corpus_version(self) -> int: ...

class SourceRegistry(Protocol):
    """Authoritative provenance store. Postgres PK(chunk_id, doc_id) arbitrates concurrency."""
    async def add(self, refs: Sequence[tuple[UUID, SourceRef]]) -> None:
        """INSERT ... ON CONFLICT (chunk_id, doc_id) DO NOTHING. Atomic, idempotent."""
    async def sources_for(self, chunk_ids: Sequence[UUID]) -> dict[UUID, list[SourceRef]]: ...
    async def remove_document(self, doc_id: str) -> list[UUID]:
        """Delete all rows for doc_id; return the affected chunk_ids for re-projection."""
    async def orphaned_chunks(self, chunk_ids: Sequence[UUID]) -> list[UUID]:
        """Subset with zero remaining source rows — safe to delete from both stores."""
```

---

## 4. `graphrag/adapters/telemetry/`

### 4.1 `telemetry/otel.py`

```python
def init_telemetry(settings: Settings, *, service_role: Literal["api","worker","cli"]) -> None:
    """Install global tracer, meter, and logger providers. Call ONCE, before anything else.

    Contract:
        - Resource attributes: service.name, service.version, service.role,
          deployment.environment, graphrag.config_hash.
        - Exporters: OTLP/gRPC to settings.observability.otlp_endpoint. Batch processors.
        - Sampler: ParentBased(TraceIdRatioBased(traces.sample_ratio)).
        - Idempotent: a second call is a no-op.
        - Never raises. If the collector is unreachable, telemetry degrades to no-op and a
          single warning is logged. Telemetry failure must NEVER fail a request.
    """

def shutdown_telemetry(timeout_s: float = 5.0) -> None:
    """Flush all pending spans/logs/metrics. Called from lifespan teardown."""

def tracer() -> Tracer: ...
def meter() -> Meter: ...
```

### 4.2 `telemetry/logging.py`

> ⚠️ **The OTel Python *logs* pipeline is experimental — traces and metrics are not.**
> The bridge lives in `opentelemetry.sdk._logs` — note the leading underscore. Upstream states
> plainly that these APIs may change in minor/patch releases with no backward-compatibility
> guarantee. The *specification* is stable; the Python implementation is not.
>
> Three consequences for this design:
> 1. **Pin OTel packages to exact versions** in `pyproject.toml` (`==`, not `>=`), and upgrade
>    them deliberately as their own commit. A routine `uv lock --upgrade` can otherwise break
>    logging on a patch bump.
> 2. **All `_logs` imports live in this one module.** Nothing else in the codebase touches them,
>    so an upstream break is a one-file fix rather than a scavenger hunt.
> 3. **Fallback if it breaks mid-build:** structlog already renders JSON to stdout. Drop the OTLP
>    handler, keep the renderer, and collect stdout with the Loki Docker logging driver. Traces
>    and metrics are unaffected, and `trace_id`/`span_id` are already inside the JSON via
>    `add_otel_context`, so correlation and the trail CLI keep working. Losing OTLP logs costs
>    you structured attributes, not correlation. Do not let this block a build order.

```python
def configure_logging(settings: Settings, *, service_role: str) -> None:
    """Install the structlog processor chain and bridge stdlib logging to OTLP.

    Processor order (exact):
        1. structlog.contextvars.merge_contextvars
        2. add_log_level
        3. TimeStamper(fmt="iso", utc=True)
        4. add_otel_context          (trace_id, span_id — only when a span is recording)
        5. add_static_fields         (service, role, env, version, config_hash)
        6. redact_secrets
        7. StackInfoRenderer, format_exc_info
        8. ConsoleRenderer if observability.logs.renderer=="console" else JSONRenderer
    """

def add_otel_context(logger, method_name, event_dict) -> dict:
    """Attach trace_id (32 hex) and span_id (16 hex) when a valid span is current.

    Contract: absent keys when no span is active — never emits nulls or zeros.
    """

def redact_secrets(logger, method_name, event_dict) -> dict:
    """Replace values whose KEY matches observability.logs.redact_patterns with '**********'.

    Contract:
        - Recurses into nested dicts and lists.
        - Also redacts any string VALUE matching a known key prefix pattern (sk-, gsk_, AIza).
        - Depth-capped at 6 to bound cost.
    """

def bind_request_context(*, correlation_id: str, **extra: str) -> None:
    """structlog.contextvars.bind_contextvars wrapper. Task-local; safe under concurrency."""

def clear_request_context() -> None: ...
```

### 4.3 `telemetry/decorators.py`

```python
def traced(name: str | None = None, *, record_args: Sequence[str] = (),
           record_result_len: bool = False) -> Callable[[F], F]:
    """Wrap an async or sync callable in a span.

    Contract:
        - name defaults to f"{module_basename}.{qualname}".
        - Records ONLY the parameters listed in record_args, by name, coerced to str and
          truncated to 200 chars. Never records **kwargs wholesale.
        - On exception: span.record_exception, status=ERROR, then re-raise unchanged.
        - Preserves signature and __wrapped__ (functools.wraps).
        - Zero-config: works on both sync and async functions; detects via iscoroutinefunction.
    """

def counted(metric: str, *, labels: Sequence[str] = ()) -> Callable[[F], F]:
    """Increment a counter on each call with outcome=success|error plus listed label values."""

def timed(metric: str, *, labels: Sequence[str] = ()) -> Callable[[F], F]:
    """Record duration in a histogram (milliseconds)."""
```

### 4.4 `telemetry/middleware.py`

```python
class CorrelationIdMiddleware:
    """PURE ASGI middleware (implements __call__(scope, receive, send)).

    MUST NOT be a Starlette BaseHTTPMiddleware subclass: BaseHTTPMiddleware runs the endpoint
    in a separate task, so contextvars bound inside the endpoint are invisible to the
    middleware afterwards, and the access log loses every enriched field.

    Contract:
        - Reads security.correlation_id_header from the request; generates a ULID if absent.
        - Binds correlation_id into structlog contextvars AND sets it as span attribute
          'app.correlation_id' (this is what the trail CLI queries on).
        - Echoes the header on the response, including error responses.
        - Clears contextvars in a finally block.
    """

class AccessLogMiddleware:
    """Pure ASGI. Emits one 'http_request' event per request.

    Fields: method, path, route_template, status, duration_ms, request_bytes, response_bytes.
    Skips paths in {"/healthz", "/readyz", "/metrics"} to avoid log spam.
    """
```

### 4.5 `telemetry/metrics.py`

```python
class Metrics:
    """All instruments declared once. Injected, never created ad hoc.

    Instruments (ARCHITECTURE §3.5):
        route_selected          Counter{strategy}
        retrieval_latency       Histogram{backend}
        repair_attempts         Histogram{node}
        answer_refused          Counter{reason}
        citations_invalid       Counter
        llm_tokens              Counter{model,role,direction}
        llm_rate_limited        Counter{provider}
        ingest_chunks_deduped   Counter
        entities_merged         Counter{band}
        grader_degraded         Counter
        projection_lag          Histogram

    Each instrument has exactly ONE emission site. A declared-but-never-emitted metric is a
    dead dashboard panel; a metric emitted from two places is impossible to reason about.

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
    def __init__(self, meter: Meter) -> None: ...
```

### 4.6 `telemetry/trail.py`

```python
class TrailBuilder:
    """Assemble a paste-ready debug bundle for one correlation_id.

    Contract:
        - Queries Loki with {service_name="graphrag"} | json | correlation_id="<cid>"
          and Tempo with TraceQL { .app.correlation_id = "<cid>" }.
          NOTE the label is `service_name`, not `service`: Loki's OTLP ingestion path maps the
          OTel resource attribute `service.name` to a label with the dot replaced by an
          underscore. Querying `{service="graphrag"}` matches nothing and returns an empty
          bundle, which looks exactly like "logs were never exported".
          Also: do NOT set `service.namespace` in the OTel resource. When it is present, Loki's
          default heuristic emits `namespace/name` as the service_name VALUE, so an equality
          match on the service name alone stops matching.
        - Merges into one list ordered by timestamp; spans and logs interleaved.
        - Truncates every field to observability.trail.truncate_field_chars.
        - Applies redact_secrets to every record before rendering.
        - Caps output at observability.trail.max_events; notes the truncation if hit.
        - If either backend is unreachable, renders what it has and states which source failed.
          Never raises.
    """
    async def build(self, correlation_id: str) -> TrailBundle: ...

class TrailBundle(BaseModel):
    correlation_id: str; trace_ids: list[str]; config_hash: str
    events: list[TrailEvent]; truncated: bool; sources_failed: list[str]
    def to_markdown(self) -> str:
        """Header (cid, config_hash, span count, failures) then a time-ordered event table,
        then full detail for every event with level >= ERROR."""
```

---

## 5. `graphrag/adapters/` — infrastructure

### 5.1 `adapters/fastembed_embedder.py`

```python
class FastEmbedEmbedder:
    """Local ONNX embeddings. Implements Embedder.

    Contract:
        - Models load lazily on first use, once per process; loading is thread-safe.
        - embed_dense prepends embedding.dense.query_prefix when is_query=True and never
          otherwise. Getting this backwards silently degrades retrieval.
        - Prepend the prefix YOURSELF via plain `embed()`. Do NOT rely on `query_embed()`:
          verified on fastembed 0.8.0, for ONNX models such as bge-small-en-v1.5 it falls
          through to the base implementation, which just calls `embed()` and applies NO prefix.
          Only certain model families override it. Calling `query_embed()` and assuming it
          prefixed is a silent, invisible retrieval regression.
        - Conversely, if a future fastembed version DOES add prefixing to `query_embed()`,
          combining it with your own prepend double-prefixes the text. Both failure modes are
          silent, so the adapter pins the behaviour with a unit test (see BO-04).
        - Batches internally at embedding.dense.batch_size regardless of input size.
        - Runs the (blocking, CPU-bound) encode in a thread via asyncio.to_thread.
        - Results are cache-read/written through the injected Cache when enabled,
          key = f"emb:{model}:{sha256(text)}".
        - embed_sparse returns TERM-FREQUENCY vectors only; IDF is applied by Qdrant.
    """
    def __init__(self, settings: EmbeddingSection, cache: Cache | None) -> None: ...
```

### 5.2 `adapters/qdrant_store.py`

```python
class QdrantVectorStore:
    """Implements VectorStore.

    Contract:
        - ensure_collections() creates 'chunks' with:
            vectors_config={"dense": VectorParams(size=dim, distance=COSINE)}
            sparse_vectors_config={"bm25": SparseVectorParams(modifier=Modifier.IDF)}
          The IDF modifier is MANDATORY and cannot be added to an existing collection.
          If the collection exists WITHOUT it, raise ConflictError naming the required action.
        - Payload indexes: doc_ids (keyword), entity_ids (keyword), schema_version (integer).
          NOT sources[].doc_id — see the payload-shape note below.
        - hybrid_search issues exactly ONE query_points call:
            prefetch=[Prefetch(query=dense, using="dense", limit=prefetch_limit),
                      Prefetch(query=sparse, using="bm25", limit=prefetch_limit)]
            query=models.RrfQuery(rrf=models.Rrf(k=rrf_k, weights=weights))
          Use RrfQuery, not FusionQuery — FusionQuery accepts no k/weights.
        - An empty sparse vector is legal and must not raise; dense-only results are returned.
        - set_sources OVERWRITES the array; it never reads-then-appends. The caller
          (ProjectionService) supplies the authoritative list from Postgres.
        - Every library exception is wrapped in RetrievalBackendUnavailable.
        - All calls carry a timeout from stores.qdrant.timeout_s.
    """
    def __init__(self, client: AsyncQdrantClient, settings: Settings) -> None: ...
```

> **Host vs container endpoints: use precedence, not two files.** Any setting naming a service by
> hostname (`observability.otlp_endpoint`, `trail.loki_url`, `trail.tempo_url`, and the entries in
> `stores.*`) has two correct values depending on who is reading it. Host-run consumers — the CLI,
> integration tests, your browser — need `localhost:<published port>`. Containerized consumers —
> `api`, `worker` — need the compose service name. Keep `localhost` in `config/local.yaml` and set
> the container values as environment variables in the compose service definitions. Env beats YAML
> in the source precedence (§5.1), so both are served from one file with no toggling. Editing the
> YAML back and forth is the failure mode this avoids: it works for whoever ran it last.

> ⚠️ **`extra` must be `"ignore"`, not `"forbid"` — and the reason is non-obvious.**
> `.env` is shared with Docker Compose, so it holds variables that are *not* `Settings` fields:
> `POSTGRES_USER`, `NEO4J_AUTH`, `LITELLM_MASTER_KEY`, and so on. pydantic-settings feeds every
> key in the dotenv file to the model, and `extra="forbid"` rejects each one — roughly a dozen
> validation errors at startup, all pointing at variables that are correct and necessary.
>
> The failure is confusing because it only appears **after** you create `.env`. Every test passes
> in a fresh clone and then the suite goes red the moment the file exists, which reads like the
> `.env` is malformed. It isn't.
>
> Switching to `"ignore"` loses nothing that mattered: `extra="forbid"` was there to catch **YAML
> typos**, and `LayeredYamlSource` now does that directly by validating merged keys against
> `model_fields`. That is strictly better — it names the offending key and the file it came from,
> instead of emitting a generic extra-field error.
>
> Do NOT solve this by prefixing the Compose variables with `GRAPHRAG_`. Compose reads them by
> their conventional names; renaming them breaks the containers to satisfy a config check.

> **Payload shape: keep a flat `doc_ids` alongside the rich `sources[]`.**
> `sources` is an array of objects, which makes it awkward to index and slow to filter:
> Qdrant documents dot notation for nested payload indexes, and whether the `field[].subfield`
> array form is supported *for index creation* is an open question upstream. Filtering via a
> `NestedCondition` is reported to be orders of magnitude slower than a flat match.
>
> So carry both:
> - `sources: [{doc_id, uri, page, char_start, char_end, ingested_at}]` — rich, for rendering
>   citations. Never filtered on, never indexed.
> - `doc_ids: ["d1", "d7"]` — a flat keyword array derived from `sources`, indexed, and the
>   only field used in filters (`FieldCondition(key="doc_ids", match=MatchValue(...))`).
>
> Both are written together by `ProjectionService`, so they cannot drift. This costs a few bytes
> per point and removes the entire nested-filter problem. Note that provenance *queries* now go
> to Postgres anyway; `doc_ids` exists only to scope a vector search to a document subset.

### 5.3 `adapters/neo4j_store.py`

```python
CYPHER_TEMPLATES: Final[dict[str, str]]
"""Whitelisted, parameterized Cypher. Keys must equal retrieval.graph.templates_enabled.

Every template:
  - uses $-parameters ONLY. No f-strings, no .format(), no string concatenation anywhere.
  - applies a per-hop degree cap INSIDE the pattern via ORDER BY r.confidence DESC
    LIMIT $per_hop_cap, not a trailing LIMIT after full expansion.
  - returns chunk_id and doc_id for every traversed relationship.

Templates: neighbors, path_between, entities_by_relation, co_mentioned, top_entities_for_chunks
"""

class Neo4jGraphStore:
    """Implements GraphStore.

    Contract:
        - ensure_schema() creates constraints/indexes idempotently (IF NOT EXISTS).
        - upsert_* use MERGE keyed on the id property; re-running over the same corpus creates
          zero new nodes and zero new relationships.
        - upsert_relations REJECTS (raises ValidationError) any Relation with a null
          chunk_id or doc_id before touching the database.
        - upsert_chunks stores the FULL Chunk.text.
        - traverse(template, params) looks template up in CYPHER_TEMPLATES by key and raises
          ValidationError on an unknown key. It never accepts raw Cypher.
        - Library exceptions wrap to GraphBackendUnavailable.
    """
    def __init__(self, driver: AsyncDriver, settings: Settings) -> None: ...
```

### 5.4 `adapters/postgres/`

`tables.py` — SQLAlchemy Core table definitions (no ORM):
`documents`, `chunk_sources`, `jobs`, `api_keys`, `eval_runs`, `eval_results`.
`chunk_sources` has `PRIMARY KEY (chunk_id, doc_id)` — this is the concurrency arbiter.

`migrations/` — Alembic. One revision per build order that changes schema.

`ledger.py`:
```python
class PostgresDocumentLedger:   # implements DocumentLedger
    """Contract:
        - register() uses INSERT ... ON CONFLICT (sha256) DO NOTHING; returns rowcount == 1.
        - set_status() validates the transition against the DocumentStatus state machine and
          raises ConflictError on an illegal move (e.g. INDEXED -> PARSING).
        - bump_corpus_version() is a single atomic UPDATE ... RETURNING.
    """

```
`sources.py`:
```python
class PostgresSourceRegistry:   # implements SourceRegistry
    """Contract:
        - add() is one executemany INSERT ... ON CONFLICT (chunk_id, doc_id) DO NOTHING.
          Safe under unlimited worker concurrency; no lock, no shard.
        - remove_document() returns affected chunk_ids so the caller can re-project.
        - orphaned_chunks() is a single query, not a per-chunk loop.
    """

```
`evals.py` (built in BO-11, not BO-03):
```python
class PostgresEvalStore:
    """run_id, git_sha, config_hash, metrics JSONB; per-question results."""
```

> **Breaker wraps retry, never the reverse.** Every adapter that talks to a backend composes them
> in one order: `circuit_breaker(retry(call))`. One logical operation then registers as **one**
> failure with the breaker after its retries are exhausted. Inverted, three retries of a dead
> backend look like three failures, `fail_max: 5` trips after two requests instead of five, and
> the breaker opens on transient noise. Retries use exponential backoff **with full jitter** —
> without jitter, every client that backed off together retries together and re-floors the
> service that just recovered.

### 5.5 `adapters/redis_cache.py`

```python
class RedisCache:
    """Implements Cache.

    Contract:
        - Namespaced keys: f"{prefix}{key}". Prefixes from cache.<tier>.prefix.
        - Retrieval-cache keys MUST embed corpus_version so ingestion auto-invalidates:
          f"ret:{sha256(query|params|config_hash)}:{corpus_version}".
        - Every method returns None / no-ops on a Redis error and increments a counter.
          A cache outage degrades performance, never correctness.
        - delete_prefix uses SCAN + batched UNLINK. Never KEYS.
    """
```

### 5.6 `adapters/litellm_client.py`

```python
# StructuredResult is defined in core/models.py (§3.2) — this adapter constructs it.

class LiteLLMClient:
    """Implements LLMClient. The ONLY component that talks to the gateway.

    Contract:
        - Holds exactly one credential: secrets.litellm_virtual_key. Provider keys must NOT
          be present in this process's environment.
        - structured() resolves role -> alias via llm.roles[role].model, then:
            1. Call with response_format json_schema when supported, else json_object.
            2. Parse into `schema`. On pydantic ValidationError, re-prompt appending the
               error text as a user message; repeat up to max_repairs.
            3. After max_repairs, raise LLMSchemaViolation with attempts in details.
          Total upstream calls are exactly max_repairs + 1. Never more.
        - Does NOT implement provider retries or fallbacks — LiteLLM owns those. Retrying here
          multiplies latency and burns free-tier quota.
        - On 429: reads Retry-After and the x-ratelimit-* headers named in
          llm.adaptive_rate_limit.read_headers, records llm_rate_limited, and raises
          RateLimited with retry_after in details. Static RPM values are never trusted.
        - Emits a span with llm.role, llm.alias, llm.model_served, llm.repair_attempts,
          llm.prompt_tokens, llm.completion_tokens.
        - Records prompts on the span only when observability.traces.record_prompts is true,
          truncated to max_recorded_prompt_chars, after redaction.
    """
```

### 5.7 `adapters/arq_queue.py`

```python
class ArqJobQueue:
    """Implements JobQueue.

    Contract:
        - enqueue() injects the W3C traceparent into envelope.otel via
          TraceContextTextMapPropagator().inject BEFORE serializing.
        - Passes the envelope as the job's first positional argument. The trace carrier does
          NOT travel in arq's ctx dict — ctx holds only redis, job_id, job_try, enqueue_time.
        - job_id is the caller's idempotency key. arq deduplicates on it, but only while the job
          is queued or its result is retained (`keep_result`) — this is NOT durable idempotency.
          The durable guarantee comes from the data layer: content-addressed chunk IDs, Cypher
          MERGE, and ON CONFLICT DO NOTHING. Treat arq's dedup as an optimisation that avoids
          redundant work, never as the correctness mechanism.
        - queue_name routes to the projection queue when specified.
    """

def restore_context(envelope: JobEnvelope[Any]) -> Context:
    """Extract the parent OTel context from envelope.otel. Used by every task wrapper."""
```

---

## 6. `graphrag/services/` — use cases, no I/O libraries

### 6.1 `services/ingestion/`

```python
# normalizer.py
def normalize_display(text: str) -> str:
    """Light cleanup for STORAGE and display: fix mojibake, normalize newlines, strip control
    chars. Preserves case, punctuation and meaningful whitespace.
    NOT the same as core.ids.normalize_for_hash — never use one where the other belongs."""

# parser.py
class ParsedDocument(BaseModel):
    text: str; title: str | None; page_offsets: list[tuple[int, int]]; mime_type: str

class DocumentParser:
    """Contract:
        - parse(bytes, mime_type) -> ParsedDocument.
        - Supports the mime types in limits.allowed_upload_mimetypes.
        - page_offsets maps page number -> (char_start, char_end) in `text`; empty for
          formats without pages.
        - Raises ValidationError for unsupported/corrupt input. Never raises library errors.
    """

# chunker.py
class ChunkSpec(BaseModel):
    text: str; ord: int; char_start: int; char_end: int; page: int | None

def chunk_document(doc: ParsedDocument, *, chunk_size: int, chunk_overlap: int,
                   min_chunk_chars: int) -> list[ChunkSpec]:
    """Recursive character splitting on paragraph -> sentence -> word boundaries.

    Contract:
        - No chunk exceeds chunk_size characters.
        - Consecutive chunks overlap by >= chunk_overlap characters, EXCEPT where a hard
          boundary makes it impossible; the final chunk may be shorter.
        - Chunks shorter than min_chunk_chars are merged into the previous chunk, not dropped,
          unless they are the only chunk.
        - char_start/char_end index into doc.text exactly: doc.text[cs:ce] == chunk.text.
        - Pure and deterministic.
    """

# service.py
class IngestionService:
    """Orchestrates one document through parse -> chunk -> embed -> store -> register sources.

    Dependencies (constructor): DocumentParser, Embedder, VectorStore, GraphStore,
                                SourceRegistry, DocumentLedger, JobQueue, Clock

    Contract of ingest(doc_id, raw, mime_type, uri):
        1. parse -> chunk -> compute chunk_id per chunk (content-addressed)
        2. Deduplicate chunk_ids WITHIN this document before any store call.
        3. Embed only chunk_ids not already present in the vector store.
        4. upsert_chunks for new chunks; existing chunks are left untouched.
        5. GraphStore.upsert_document + upsert_chunks (full text).
        6. SourceRegistry.add(...) — this is what makes multi-source retention work.
        7. Enqueue ProjectPayload for all touched chunk_ids, and ExtractEntities.
        8. Ledger status transitions at each step; bump_corpus_version at the end.
        - Idempotent: re-running for the same doc_id produces no duplicate chunks, no duplicate
          source rows, and no duplicate graph nodes.
        - It NEVER writes Qdrant's sources[] directly. That is ProjectionService's job.
    """

class ProjectionService:
    """Re-derives Qdrant sources[] from Postgres. Runs single-concurrency.

    Contract of project(chunk_ids):
        - reg.sources_for(chunk_ids) -> authoritative lists
        - For each chunk with >=1 source: VectorStore.set_sources(chunk_id, sources)
        - For each chunk with 0 sources: delete from VectorStore AND GraphStore
        - Convergent: running it twice yields the identical payload; running it over a
          hand-corrupted payload repairs it. This doubles as the reconciliation job.
        - Must be safe to run concurrently with ingestion (it only ever overwrites with truth).
    """

class DeletionService:
    """Contract of delete(doc_id):
        - SourceRegistry.remove_document(doc_id) -> affected chunk_ids
        - ProjectionService.project(affected)   (removes orphans from both stores)
        - GraphStore.delete_document(doc_id)
        - Ledger status -> DELETING then removed; bump_corpus_version
        - Runs as a background job only. Never called from a request handler.
    """
```

### 6.2 `services/resolution/`

```python
# normalize.py
def normalize_entity_name(name: str, *, strip_suffixes: Sequence[str],
                          strip_honorifics: Sequence[str]) -> str:
    """Contract:
        - NFKC -> casefold -> strip punctuation -> collapse whitespace
        - Remove honorifics only at the START, as whole tokens.
        - Remove legal suffixes only at the END, as whole tokens.
        - Whole-token matching is mandatory: "Corporation Street" keeps "corporation".
        - Idempotent. Never returns an empty string; falls back to the casefolded original.
    """

# blocking.py
class Blocker:
    """Candidate generation. Avoids O(n^2).

    Contract of candidates(mention, ...) -> list[Entity]:
        - Union of (a) exact normalized-name match and (b) vector kNN over the entities
          collection, top-k = resolution.block_k, filtered by type when require_type_match.
        - Total pairwise comparisons across n mentions must be <= n * block_k * 1.2.
    """

# scoring.py
def score_pair(a_name: str, b_name: str, a_type: EntityType, b_type: EntityType,
               cosine: float, weights: ScorerWeights, require_type_match: bool) -> float:
    """Contract:
        - Returns 0.0 immediately when require_type_match and types differ. No exceptions.
        - Otherwise w1*jaro_winkler + w2*token_set_ratio + w3*cosine, all in [0,1].
        - Pure, deterministic, symmetric: score(a,b) == score(b,a).
    """

def decide(score: float, merge_t: float, reject_t: float) -> Literal["merge","gray","reject"]: ...

# clustering.py
def cluster(pairs: Sequence[tuple[UUID, UUID]], *, max_cluster_size: int) -> list[set[UUID]]:
    """Union-find over merge edges; returns transitively closed clusters.

    Contract:
        - A~B and B~C implies one cluster {A,B,C}.
        - Raises ValidationError if any cluster exceeds max_cluster_size (tripwire for a
          mis-tuned threshold silently merging everything).
    """

def choose_canonical(members: Sequence[Mention]) -> str:
    """Most frequent surface form; ties broken by longest, then lexicographically. Deterministic."""

# service.py
class ResolutionService:
    """Contract of resolve(mentions) -> ResolutionResult:
        normalize -> block -> score -> decide -> cluster -> choose canonical
        - Emits entities_merged{band} metrics for merge/gray/reject.
        - gray_band_action='flag' records the pair in the result but does NOT merge.
        - Idempotent over the same corpus: zero new canonical_ids on a re-run.
        - Returns entities, alias edges (loser -> canonical, never deleted), and flagged pairs.
    """
```

### 6.3 `services/retrieval/`

```python
# linker.py
class EntityLinker:
    """Map free-text query mentions to canonical entities.

    Contract of link(query, top_k, min_score) -> list[Entity]:
        - Embeds candidate spans, kNN against the entities collection.
        - Drops matches below retrieval.graph.entity_link_min_score.
        - Returns [] when nothing matches. A miss is NOT an error.
    """

# vector.py
class VectorRetriever:
    """Contract of retrieve(query, top_k) -> list[ScoredChunk]:
        - Embeds the query with is_query=True.
        - One hybrid_search call; results carry origin='vector' and dense 1-based ranks.
        - Read-through cache keyed on (query, params, config_hash, corpus_version).
        - Raises RetrievalBackendUnavailable on store failure — the caller decides to degrade.
    """

# graph.py
class GraphRetriever:
    """Contract of retrieve(plan, max_hops) -> list[GraphPath]:
        - Links plan.seed_entities; returns [] if none link.
        - Selects a template by plan.strategy and passes typed params including
          per_hop_cap = retrieval.graph.max_degree_per_hop.
        - Truncates to retrieval.graph.max_paths, ranked by path score.
        - Hydrates chunk text via retrieval.graph.hydrate_from ('neo4j' by default, which is
          what makes the vector-outage fallback real).
    """

# fusion.py
def reciprocal_rank_fusion(lists: Sequence[Sequence[ScoredChunk]], *, k: int,
                           weights: Sequence[float], top_n: int) -> list[ScoredChunk]:
    """Cross-store RRF. Pure function.

    Contract:
        - score(d) = sum_i weights[i] / (k + rank_i(d)); ranks are 1-based.
        - Documents absent from a list contribute nothing from that list.
        - An empty input list is legal and contributes nothing (sparse legitimately returns 0).
        - Deterministic tie-break: higher score, then lower best rank, then chunk_id ascending.
        - Returns top_n with origin='fused' and recomputed 1-based ranks.
    """
```

### 6.4 `services/orchestration/`

```python
# state.py
def merge_counters(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    """Key-wise addition over the union of keys. Associative and commutative."""

class QueryState(TypedDict):
    """LangGraph channel schema.

    Reducer rules — get these wrong and the graph breaks in ways unit tests miss:
        - A key written by exactly ONE node needs no reducer (default last-write-wins).
        - A key written by MULTIPLE nodes needs a reducer, or updates are silently lost.
        - A key written by nodes that run IN PARALLEL needs a reducer, or LangGraph raises
          InvalidUpdateError at runtime.
        - `spent` accumulates CONSUMPTION. Never store 'remaining' — merging remainders with
          min() discards one branch's spend and leaks quota on every query.
        - Reducers receive (current, update) and nodes return only their DELTA. To count an
          attempt, a node returns {"attempts": {"grade_context": 1}} — never the running total,
          which would be added to itself.

    Why `question` and `active_query` are separate:
        `rewrite_query` loops back into `plan_route`, so a single mutable `question` would be
        overwritten by the rewrite. The user's original wording is then gone — and it is exactly
        what the final answer must address, what the golden set compares against, and what
        belongs on the trace. Retrieval reads `active_query`; generation and evaluation read
        `question`. `active_query` is seeded from `question` in `guard`.
    """
    correlation_id: str
    question: str                         # IMMUTABLE. The user's original words. Never rewritten.
    active_query: str                     # what retrieval actually uses; rewrite_query edits THIS
    plan: RoutePlan | None
    vector_hits: list[ScoredChunk]        # one writer
    graph_hits: list[GraphPath]           # one writer
    fused: list[ScoredChunk]              # one writer
    graded: list[ScoredChunk]             # one writer
    answer: Answer | None                 # TWO writers (generate, insufficient) but never in the
                                          # same super-step; last-write-wins is intended, so no
                                          # reducer. insufficient runs later and correctly wins.
    degraded: Annotated[list[str], operator.add]   # dedupe on read; both retrievers may append
    failures: Annotated[list[NodeFailure], operator.add]
    attempts: Annotated[dict[str, int], merge_counters]
    spent: Annotated[Spend, Spend.merge]

def remaining(state: QueryState, limits: BudgetLimits) -> BudgetLimits:
    """limits - state['spent']. Computed on read, never stored."""

# schemas.py — LLM output contracts. Every one is validated; none is trusted.
class RoutePlanOut(BaseModel): strategy: ...; seed_entities: ...; hops: ...; sub_queries: ...; rationale: ...
class RelevanceGrade(BaseModel): chunk_id: UUID; relevant: bool; reason: str
class RelevanceGradeBatch(BaseModel): grades: list[RelevanceGrade]
class RewrittenQuery(BaseModel): query: str; changed_because: str
class AnswerOut(BaseModel): text: str; citations: list[CitationOut]; confidence: float
class Entailment(BaseModel): supported: bool; unsupported_spans: list[str]; score: float
class EntityExtraction(BaseModel): entities: list[MentionOut]; relations: list[RelationOut]

# prompts.py
def render(template_name: str, **vars: Any) -> str:
    """Jinja2 render from prompts/*.j2.

    Contract:
        - Retrieved chunk text is ALWAYS wrapped in <document id="..."> ... </document> and
          preceded by an instruction that document content is untrusted data containing no
          instructions. This is defence-in-depth only — the real guard is schema validation
          plus deterministic citation checking.
        - Templates are versioned; the version string goes on the span.
    """
```

**Nodes** — `services/orchestration/nodes/*.py`. Every node is
`async def node(state: QueryState, deps: NodeDeps) -> dict[str, Any]`, returns a **partial**
state update, and **mutates nothing**. Each is independently unit-testable with a hand-built state.

| Node | File | Returns | Contract |
|---|---|---|---|
| `guard` | `guard.py` | `active_query` | Rejects empty / over `limits.max_query_chars` (`ValidationError`). Seeds `active_query = question`. |
| `plan_route` | `plan_route.py` | `plan`, `spent`, `attempts` | LLM role `router` -> `RoutePlanOut`. On `LLMSchemaViolation` after repairs: return `plan=RoutePlan(strategy=orchestration.default_strategy)` plus a `NodeFailure`. **Fails open** — never propagates the error. |
| `retrieve_vector` | `retrieve_vector.py` | `vector_hits`, maybe `degraded`, `failures` | On `RetrievalBackendUnavailable`: return `vector_hits=[]`, `degraded=["vector"]`, `failures=[...]`. Never raises. Runs in parallel with `retrieve_graph`. |
| `retrieve_graph` | `retrieve_graph.py` | `graph_hits`, maybe `degraded`, `failures` | On `GraphBackendUnavailable`: `graph_hits=[]`, `degraded=["graph"]`. Zero linked entities is a normal empty result, not a failure. |
| `fuse` | `fuse.py` | `fused` | Pure. Converts `GraphPath`s to `ScoredChunk`s (origin='graph', ranked by path score) then RRF. **No LLM call.** If both inputs are empty, returns `fused=[]`. |
| `grade_context` | `grade_context.py` | `graded`, `spent`, `attempts` | LLM role `grader`, batched at `orchestration.grader.batch_size`. On failure with `fail_open=true`: treat all as relevant, emit `grader_degraded`. |
| `rewrite_query` | `rewrite_query.py` | `active_query`, `attempts`, `spent` | LLM role `router`. Writes `active_query` ONLY — never `question`. Bounded by `orchestration.max_query_rewrites`. |
| `generate` | `generate.py` | `answer`, `spent`, `attempts` | LLM role `synth` -> `AnswerOut`. Prompted with `question` (the user's actual ask), grounded on `graded`. |
| `verify_citations` | `verify_citations.py` | `{}` or `failures` | **Deterministic, no LLM.** Every `answer.citations[].chunk_id` must be in `{c.chunk.chunk_id for c in graded}`. On violation: increment `citations_invalid`, append a `NodeFailure` with the offending IDs. |
| `verify_grounded` | `verify_grounded.py` | `{}` or `failures`, `spent` | LLM role `judge` -> `Entailment`. Fails if `score < min_groundedness_score`. |
| `repair` | `repair.py` | `attempts`, maybe `failures` | Pure routing bookkeeping; contains no LLM call. |
| `finalize` | `finalize.py` | `{}` | Terminal. |
| `insufficient` | `insufficient.py` | `answer` | Builds a refusal citing what *was* retrieved. Increments `answer_refused{reason}`. **This is a success path, not an error path** — HTTP 200. |

```python
# graph.py
def build_query_graph(deps: NodeDeps, settings: Settings) -> CompiledStateGraph:
    """Assemble and compile the state machine.

    Edges:
        START -> guard -> plan_route
        plan_route -> (conditional) retrieve_vector | retrieve_graph | BOTH (parallel fan-out)
        retrieve_* -> fuse -> grade_context
        grade_context -> (conditional) generate | rewrite_query | insufficient
        rewrite_query -> plan_route
        generate -> verify_citations -> (conditional) verify_grounded | repair
        verify_grounded -> (conditional) finalize | repair
        repair -> (conditional) generate | insufficient
        finalize, insufficient -> END

    Contract:
        - Every loop is bounded by a counter in `attempts` compared against config.
        - A budget check runs before every LLM-calling node; on breach, route to `insufficient`.
        - Compiled once at startup and reused; it is stateless between invocations.
    """

class NodeDeps(BaseModel):
    """Everything nodes need. Passed to every node; nodes never reach for globals."""
    llm: LLMClient; vector: VectorRetriever; graph: GraphRetriever
    linker: EntityLinker; metrics: Metrics; settings: Settings; clock: Clock

class OrchestrationService:
    """Contract:
        - run(question, correlation_id) -> QueryResult
        - stream(question, correlation_id) -> AsyncIterator[StreamEvent] emitting
          node_start / node_end / token / done when orchestration.streaming.emit_node_events.
        - Translates terminal state into a QueryResult carrying answer, citations, route,
          degraded[], spent, and correlation_id.
    """
```

---

## 7. `graphrag/apps/`

### 7.1 `apps/api/`

```python
# deps.py
def get_container(request: Request) -> Container:
    """Reads request.app.state.container.

    A FastAPI dependency has NO implicit access to lifespan state — it must take Request.
    """
async def require_api_key(request: Request, container: Container = Depends(get_container)) -> ApiKey: ...
async def require_admin(...) -> ApiKey: ...

# main.py
class Container:
    """Composition root. Built once in lifespan; owns every adapter's lifecycle.

    Contract:
        - create(settings) constructs clients, calls ensure_collections/ensure_schema, and
          returns a ready container. Raises on any failure — the process must not start with
          a half-built container.
        - aclose() closes pools in reverse construction order.
        - The worker builds its own Container in arq's on_startup. Same class, same settings.
    """

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Order: get_settings() -> init_telemetry -> configure_logging -> Container.create
    -> app.state.container -> yield -> Container.aclose -> shutdown_telemetry."""

def create_app() -> FastAPI:
    """Middleware order, OUTERMOST first:
        1. CorrelationIdMiddleware
        2. AccessLogMiddleware
        3. CORS
        4. RateLimitMiddleware
       Then OTel FastAPI instrumentation, exception handlers, routers.
       All middleware are pure ASGI — no BaseHTTPMiddleware.
    """

# errors.py
def install_exception_handlers(app: FastAPI) -> None:
    """Maps AppError -> ErrorEnvelope with its code/status/retryable.
    Unhandled Exception -> INTERNAL_ERROR 500: logs the full traceback, returns NO traceback.
    Every response carries correlation_id and trace_id."""

class ErrorEnvelope(BaseModel):
    error: ErrorBody      # code, message, correlation_id, trace_id, retryable, details
```

**Routers** (`apps/api/routers/`):

| Route | File | Contract |
|---|---|---|
| `POST /v1/query` | `query.py` | Body `QueryRequest{question, top_k?, strategy?}`. Returns `QueryResponse{answer, citations, route, degraded, spent, correlation_id}`. Refusals are **200**. |
| `POST /v1/query/stream` | `query.py` | SSE. Events: `node_start`, `node_end`, `token`, `done`, `error`. |
| `POST /v1/documents` | `documents.py` | Multipart. Requires `Idempotency-Key`. Validates MIME by **content sniffing**, not the declared header, plus size cap. Returns **202** `{job_id, doc_id, correlation_id}`. |
| `DELETE /v1/documents/{id}` | `documents.py` | Enqueues `DeleteDocument`. Returns **202**. Never deletes inline. |
| `GET /v1/jobs/{id}` | `jobs.py` | Job state + document status. |
| `GET /v1/debug/trail/{cid}` | `debug.py` | Admin key required. Returns 404 when `app.env == "prod"` or `trail.enabled` is false. |
| `GET /healthz` `/readyz` | `health.py` | `healthz` = process alive; always 200 while serving, no I/O. `readyz` = backend probes; 503 listing the failing names. **Never conflate them** — a liveness probe that fails on a dependency blip restarts a healthy process. `readyz` caches its result for 5s and probes all backends concurrently with `asyncio.TaskGroup` under a 2s deadline, so a 1s poll interval can't turn into a fan-out amplifier or hang on one slow backend. |

### 7.2 `apps/worker/`

```python
# settings.py
class WorkerSettings:
    """arq worker configuration.

    Contract:
        - functions = [ingest_document, extract_entities, resolve_entities, delete_document]
        - on_startup builds the Container and stores it on ctx; on_shutdown closes it.
        - max_jobs = ingestion.parallelism.max_concurrent_docs
        - retry_jobs=True, max_tries from ingestion.dead_letter.max_attempts
    """

class ProjectionWorkerSettings:
    """SEPARATE worker process for the projection queue.

    Contract:
        - queue_name = ingestion.payload_projection.queue_name
        - max_jobs = 1. This single value is what removes the read-modify-write race;
          any value > 1 reintroduces it.
    """

# tasks/ingest.py, tasks/project.py, tasks/delete.py, tasks/extract.py, tasks/resolve.py
# One task per file. Every task has this exact shape:
async def ingest_document(ctx: dict, env: JobEnvelope[IngestDocumentPayload]) -> None:
    """`ctx` is arq's dict (redis, job_id, job_try, enqueue_time) — it does NOT carry the
    payload. The envelope arrives as a positional argument.

    Contract (identical for all tasks):
        - Reject envelopes whose schema_version major differs from SCHEMA_VERSION.
        - restore_context(env) -> start a span as its child, so the trace spans the queue hop.
        - bind_request_context(correlation_id=..., job_id=ctx['job_id'], job_try=ctx['job_try'])
        - On final failure: set ledger status FAILED with the error code, then re-raise so arq
          records it.
    """
```

### 7.3 `apps/cli/main.py`

Typer app. Each command builds its own `Container`, runs, and closes it.

| Command | Flags | Notes |
|---|---|---|
| `ingest <path>` | `--recursive` | File or directory. Enqueues jobs; `--wait` blocks until INDEXED |
| `query <text>` | `--show-chunk-ids`, `--json`, `--strategy` | `--show-chunk-ids` prints each retrieved chunk's UUID beside its text. **This is how gold_chunk_ids are collected for the golden set (MANUAL M-5)** — nobody transcribes UUIDs by hand |
| `trail <cid>` | `--out` | Writes `debug_bundle_<cid>.md`. Built in BO-02 |
| `eval` | `--subset`, `--report` | `--subset` runs `evaluation.smoke_subset_size` items |
| `reindex` | `--force` | Re-projects Qdrant payloads from Postgres; the reconciliation entrypoint |
| `config-hash` | — | Prints the resolved `config_hash`. Use it to confirm two environments match |
| `seed` | — | Ingests `corpus/` |

---

## 8. `graphrag/evaluation/`

```python
# golden/*.yaml  (you supply — see MANUAL)
class GoldenItem(BaseModel):
    id: str
    question: str
    category: Literal["single_hop","multi_hop","thematic","unanswerable"]
    gold_route: Literal["vector","graph","hybrid"]
    gold_chunk_ids: list[UUID]       # empty for unanswerable
    gold_answer: str | None          # None for unanswerable
    must_refuse: bool = False

# metrics/retrieval.py — pure functions, no LLM, exactly testable
def recall_at_k(retrieved: Sequence[UUID], gold: Sequence[UUID], k: int) -> float: ...
def mrr(retrieved: Sequence[UUID], gold: Sequence[UUID]) -> float: ...
def ndcg_at_k(retrieved: Sequence[UUID], gold: Sequence[UUID], k: int) -> float:
    """Binary relevance. Monotonic: improving an item's rank never lowers the score."""

# metrics/routing.py
def routing_accuracy(pred: Sequence[str], gold: Sequence[str]) -> tuple[float, ConfusionMatrix]: ...

# metrics/generation.py
class GenerationJudge:
    """RAGAS + Phoenix wrappers. Contract: uses llm.roles['judge'], whose provider is
    validated at startup to differ from llm.roles['synth'] (self-preference bias)."""

# runner.py
class EvalRunner:
    """Contract:
        - Sets cache.<tier>.enabled = False for the run when
          evaluation.disable_cache_during_run — otherwise run 2 scores the cache.
        - Executes every GoldenItem, collects per-item results.
        - Persists an eval_runs row with git_sha AND config_hash.
        - Returns EvalReport; `--subset` runs evaluation.smoke_subset_size items for CI.
        - Fails the process with exit 1 if any metric is below evaluation.thresholds.
    """

# report.py
def render_markdown(report: EvalReport) -> str:
    """Metric table with pass/fail against thresholds, per-category breakdown, and the
    refusal rate on unanswerable items called out separately."""
```

---

## 9. Test Infrastructure — `tests/`

```python
# fakes.py — in-memory implementations of every port. NO mocks, NO MagicMock.
class FakeClock(Clock)              # settable; advance(seconds)
class FakeIdGenerator(IdGenerator)  # deterministic counter
class FakeEmbedder(Embedder)        # hash-derived deterministic vectors
class FakeVectorStore(VectorStore)  # dict-backed; supports a failure-injection flag
class FakeGraphStore(GraphStore)
class FakeLLMClient(LLMClient)      # scripted responses queue; can emit invalid JSON N times
class FakeCache, FakeJobQueue, FakeSourceRegistry, FakeDocumentLedger

# factories.py — build valid domain objects with sensible defaults
def make_chunk(text: str = ..., sources: list[SourceRef] | None = None) -> Chunk: ...
def make_state(**overrides: Any) -> QueryState: ...

# conftest.py
@pytest.fixture def settings() -> Settings          # loads config/test.yaml
@pytest.fixture def container() -> Container        # all-fakes container
@pytest.fixture(scope="session") def stack()        # docker compose up for integration tests
```

**Markers:** `unit` (default, no I/O), `integration` (needs `stack`), `contract`, `eval` (needs
LLM keys, excluded from default runs).

**Rule:** a unit test that needs a running container is misfiled. Move it to `integration/` or
replace the dependency with a fake.
