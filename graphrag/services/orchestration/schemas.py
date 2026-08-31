"""LLM output contracts. See BLUEPRINT §6.4.

Every one of these is validated by pydantic on the way out of `LLMClient.structured()`; none is
trusted. Rules for ALL `*Out` schemas (and the other LLM-facing models below):
    - Identifiers are `str`, never UUID. The model emits text; the NODE converts and validates.
      A UUID-typed field turns a hallucinated id into a parse failure and burns a repair attempt,
      when the right handling is a deterministic membership check (`verify_citations`).
    - Every field has an explicit `description=` — it becomes the JSON-schema description the
      provider sees, and is the cheapest quality lever available.
    - Bounded fields carry constraints (Literal, ge/le, max_length) so a malformed value fails
      validation instead of flowing downstream.
    - No Optional fields. An absent value the model chose not to emit is indistinguishable from
      one it couldn't determine; require it and let the repair loop handle refusal.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from graphrag.core.models import EntityType


class MentionOut(BaseModel):
    # `chunk_id` (BO-07 addition — see `services/resolution/../apps/worker/tasks/extract.py`'s
    # module docstring for why): BLUEPRINT §6.4's sketch for this schema has no such field
    # because its `extract_entities.j2` sketch renders exactly one chunk per call. BUILD_ORDER
    # BO-07 requires batching many chunks into one `bulk`-role call (the RPM-bound role — see
    # ARCHITECTURE §4.3), which makes offsets alone ambiguous across documents in the same
    # prompt. Follows the *Out schema rule already stated below (str, not UUID) and the existing
    # multi-document prompt convention (`chunks=[{chunk_id, text}, ...]`, see grade_context.j2 /
    # generate.j2) rather than inventing a new one.
    chunk_id: str = Field(description="Exactly one of the document ids given in the prompt")
    surface: str = Field(max_length=200, description="Exact text as it appears in the chunk")
    type: EntityType
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)


class RelationOut(BaseModel):
    # See `MentionOut.chunk_id` above for why this field exists beyond BLUEPRINT's sketch.
    chunk_id: str = Field(description="Exactly one of the document ids given in the prompt")
    src_surface: str = Field(description="Surface form of the source entity, not an id")
    dst_surface: str = Field(description="Surface form of the destination entity, not an id")
    type: str = Field(max_length=64, description="UPPER_SNAKE verb phrase, e.g. ACQUIRED")
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_span: str = Field(
        max_length=500, description="Verbatim sentence supporting this relation"
    )


class CitationOut(BaseModel):
    chunk_id: str = Field(description="Exactly one of the ids given in the context block")
    quote: str | None = Field(default=None, max_length=300)


class RoutePlanOut(BaseModel):
    strategy: Literal["vector", "graph", "hybrid"]
    template: str = Field(
        default="neighbors",
        description="Cypher template to use: neighbors, path_between, entities_by_relation, "
        "co_mentioned, top_entities_for_chunks",
    )
    seed_entities: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Entity names mentioned in the question; empty for non-entity queries",
    )
    hops: int = Field(ge=1, le=3)
    relation_type: str | None = Field(
        default=None, description="Only required for entities_by_relation template"
    )
    sub_queries: list[str] = Field(default_factory=list, max_length=4)
    rationale: str = Field(max_length=400)


class RelevanceGrade(BaseModel):
    chunk_id: str = Field(description="The id of the chunk being graded")
    relevant: bool
    reason: str = Field(max_length=200)


class RelevanceGradeBatch(BaseModel):
    grades: list[RelevanceGrade]


class RewrittenQuery(BaseModel):
    query: str = Field(max_length=500)
    changed_because: str


class AnswerOut(BaseModel):
    text: str
    citations: list[CitationOut] = Field(
        min_length=1,
        description="At least one. If the context does not support an answer, say so in `text` "
        "and cite the closest chunk rather than inventing an id.",
    )
    confidence: float = Field(ge=0.0, le=1.0)


class Entailment(BaseModel):
    supported: bool
    unsupported_spans: list[str] = Field(default_factory=list)
    score: float = Field(ge=0.0, le=1.0)


class EntityExtraction(BaseModel):
    entities: list[MentionOut]
    relations: list[RelationOut]
