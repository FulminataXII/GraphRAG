"""Single source of truth for all configuration. See BLUEPRINT §2.2.

Nothing else in the codebase calls `os.environ` directly — this module is the only place
env/YAML/secrets are read.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import computed_field, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from graphrag.config.schema import (
    AppSection,
    CacheSection,
    EmbeddingSection,
    EvaluationSection,
    IngestionSection,
    LimitsSection,
    LLMSection,
    ObservabilitySection,
    OrchestrationSection,
    ResilienceSection,
    ResolutionSection,
    RetrievalSection,
    SecretsSection,
    SecuritySection,
    StoresSection,
)

_CONFIG_DIR = Path(__file__).resolve().parent


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursive dict merge. Overlay wins. Lists and scalars replace, never merge."""
    result: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        base_value = result.get(key)
        if isinstance(base_value, Mapping) and isinstance(value, Mapping):
            result[key] = deep_merge(base_value, value)
        else:
            result[key] = value
    return result


class LayeredYamlSource(PydanticBaseSettingsSource):
    """Deep-merging multi-file YAML source. Do NOT use the stock YamlConfigSettingsSource here.

    Why this exists:
        pydantic-settings merges a list of config files SHALLOWLY. Given
        `yaml_file=["base.yaml", "local.yaml"]`, a `local.yaml` containing only
        `observability.logs.level` REPLACES the whole `observability` mapping rather than
        overriding one leaf. Partial environment overrides — the entire point of
        base/local/prod layering — do not work with the stock source.

    Contract:
        - Reads each path in order; a missing path is skipped silently (prod.yaml need not
          exist in dev), but a malformed one raises.
        - Deep-merges dict values recursively; later files win.
        - Scalars and lists are replaced wholesale, never concatenated.
        - Returns the merged mapping. Validation is pydantic's job, not this source's.
        - Pure and side-effect free: same files in, same dict out.
    """

    def __init__(self, settings_cls: type[BaseSettings], paths: Sequence[Path]) -> None:
        super().__init__(settings_cls)
        self._paths = list(paths)
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for path in self._paths:
            if not path.is_file():
                continue
            try:
                content = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise ValueError(f"malformed YAML in {path}: {exc}") from exc
            if content is None:
                continue
            if not isinstance(content, dict):
                raise ValueError(f"{path} must contain a mapping at the top level")
            merged = deep_merge(merged, content)

        unknown = sorted(set(merged) - set(self.settings_cls.model_fields))
        if unknown:
            raise ValueError(
                f"unknown top-level key(s) in YAML config: {unknown} — Settings.model_config "
                "uses extra='ignore' (so .env can carry Docker Compose variables that aren't "
                "Settings fields), so this check is what still catches a YAML typo at startup"
            )
        return merged

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._data)


class Settings(BaseSettings):
    """Single source of truth for all configuration.

    Contract:
        - Constructed exactly once per process, via get_settings().
        - Immutable (frozen). Never model_copy() it — cached_property would carry a stale hash.
        - Unknown top-level keys in YAML raise ValueError at construction — enforced by
          LayeredYamlSource, not by extra="forbid" here. extra is "ignore" because .env is
          shared with Docker Compose and legitimately carries variables (POSTGRES_USER, etc.)
          that aren't Settings fields; env/.env sources silently drop what they don't
          recognize, exactly like Compose itself does. Typos WITHIN a section (e.g.
          `retrieval.vector.top_kk`) are still caught — every section model in
          config/schema.py keeps its own extra="forbid".
        - Precedence, highest first: init > env > .env > YAML > field defaults.
        - config_hash covers everything EXCEPT `secrets`; it is safe to log and to attach
          to telemetry.
    """

    model_config = SettingsConfigDict(
        env_prefix="GRAPHRAG_",
        env_nested_delimiter="__",
        env_file=".env",
        frozen=True,
        extra="ignore",
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
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order is highest-priority-first. Note the CUSTOM yaml source."""
        app_env = os.getenv("APP_ENV", "local")
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            LayeredYamlSource(
                settings_cls,
                paths=[_CONFIG_DIR / "base.yaml", _CONFIG_DIR / f"{app_env}.yaml"],
            ),
            file_secret_settings,
        )

    @model_validator(mode="after")
    def _cross_section(self) -> Settings:
        """Validate invariants that span two sections.

        NOT checked here: that embedding.dense.dimensions matches the real model's output.
        Confirming that requires loading the model — I/O in a pure config object. It is
        asserted in Container.create() instead, before ensure_collections().
        """
        judge = self.llm.roles["judge"]
        synth = self.llm.roles["synth"]
        if judge.model == synth.model:
            raise ValueError(
                "llm.roles['judge'].model must differ from llm.roles['synth'].model "
                "(LLM-as-judge self-preference bias)"
            )
        if self.resilience.degradation.allow_graph_only and not self.stores.neo4j.store_chunk_text:
            raise ValueError(
                "resilience.degradation.allow_graph_only requires stores.neo4j.store_chunk_text "
                "— otherwise the graph path returns chunk_ids with no readable text"
            )
        if self.app.env == "prod" and self.observability.trail.enabled:
            raise ValueError("observability.trail.enabled must be false when app.env == 'prod'")
        if self.app.env == "prod" and not self.security.debug_endpoints_require_admin:
            raise ValueError(
                "security.debug_endpoints_require_admin must be true when app.env == 'prod'"
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @cached_property
    def config_hash(self) -> str:
        """sha256 of model_dump(mode='json', exclude={'secrets'}), keys sorted. 64 hex chars.

        `config_hash` is itself a computed_field, so it appears in a plain model_dump() —
        it must be excluded here too, or dumping recurses into computing the hash forever.
        """
        payload = self.model_dump(mode="json", exclude={"secrets", "config_hash"})
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. The ONLY construction site for Settings outside tests."""
    return Settings()
