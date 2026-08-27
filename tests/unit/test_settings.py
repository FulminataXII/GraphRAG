from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from graphrag.config.settings import Settings, deep_merge
from tests.unit._settings_helpers import make_isolated_config_dir, set_required_secrets


@pytest.fixture(autouse=True)
def _secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required_secrets(monkeypatch)


def test_settings_loads_defaults() -> None:
    settings = Settings()
    assert re.fullmatch(r"[0-9a-f]{64}", settings.config_hash)


def test_settings_rejects_unknown_key(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = make_isolated_config_dir(tmp_path, monkeypatch)
    base_path = config_dir / "base.yaml"
    base_path.write_text(base_path.read_text() + "\nfoo: 1\n")
    with pytest.raises(ValueError, match="unknown top-level key"):
        Settings()


def test_env_file_extra_keys_ignored(tmp_path) -> None:
    """.env is shared with Docker Compose and legitimately carries variables — like
    POSTGRES_USER — that aren't Settings fields. extra="ignore" must not reject them.

    The autouse tests/conftest.py fixture already chdir'd us into this same tmp_path."""
    (tmp_path / ".env").write_text("POSTGRES_USER=x\n")
    settings = Settings()
    assert settings.config_hash


def test_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAPHRAG_RETRIEVAL__VECTOR__TOP_K", "99")
    settings = Settings()
    assert settings.retrieval.vector.top_k == 99


def test_precedence_env_beats_yaml(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = make_isolated_config_dir(tmp_path, monkeypatch)
    (config_dir / "envwins.yaml").write_text("retrieval:\n  vector:\n    top_k: 5\n")
    monkeypatch.setenv("APP_ENV", "envwins")
    monkeypatch.setenv("GRAPHRAG_RETRIEVAL__VECTOR__TOP_K", "42")
    settings = Settings()
    assert settings.retrieval.vector.top_k == 42


def test_partial_env_override_deep_merges(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A local.yaml overriding one leaf under observability must leave every other key in
    that section intact. The stock YamlConfigSettingsSource merges file lists shallowly and
    would blow away the whole section — this is what proves LayeredYamlSource is wired in."""
    config_dir = make_isolated_config_dir(tmp_path, monkeypatch)
    (config_dir / "partial.yaml").write_text("observability:\n  logs:\n    level: DEBUG\n")
    monkeypatch.setenv("APP_ENV", "partial")

    overridden = Settings()

    assert overridden.observability.logs.level == "DEBUG"
    # Every other key in observability.logs and in observability itself survives from base.yaml.
    assert overridden.observability.logs.renderer == "json"
    assert overridden.observability.logs.redact_patterns
    assert overridden.observability.otlp_endpoint == "http://otel-collector:4317"
    assert overridden.observability.traces.enabled is True
    assert overridden.observability.phoenix.enabled is True


def test_deep_merge_replaces_lists_not_appends() -> None:
    base = {"cors_origins": ["https://base.example"], "kept": {"a": 1, "b": 2}}
    overlay = {"cors_origins": ["https://local.example"]}
    merged = deep_merge(base, overlay)
    assert merged["cors_origins"] == ["https://local.example"]
    assert merged["kept"] == {"a": 1, "b": 2}


def test_deep_merge_merges_nested_dicts() -> None:
    base = {"observability": {"logs": {"level": "INFO", "renderer": "json"}, "otlp": "x"}}
    overlay = {"observability": {"logs": {"level": "DEBUG"}}}
    merged = deep_merge(base, overlay)
    assert merged["observability"]["logs"] == {"level": "DEBUG", "renderer": "json"}
    assert merged["observability"]["otlp"] == "x"


def test_missing_env_yaml_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")  # config/prod.yaml does not exist yet
    settings = Settings()
    assert settings.app.env == "local"  # falls back to base.yaml's value


def test_malformed_yaml_raises(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = make_isolated_config_dir(tmp_path, monkeypatch)
    (config_dir / "broken.yaml").write_text("foo: [1, 2\n  bar: 3\n")
    monkeypatch.setenv("APP_ENV", "broken")
    with pytest.raises(ValueError, match="malformed YAML"):
        Settings()


def test_cross_section_judge_differs_from_synth(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = make_isolated_config_dir(tmp_path, monkeypatch)
    (config_dir / "samejudge.yaml").write_text(
        "llm:\n  roles:\n    judge:\n      model: synth-quality\n"
        "      temperature: 0.0\n      max_tokens: 500\n"
    )
    monkeypatch.setenv("APP_ENV", "samejudge")
    with pytest.raises(ValidationError, match="judge"):
        Settings()


def test_cross_section_graph_only_requires_chunk_text(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = make_isolated_config_dir(tmp_path, monkeypatch)
    (config_dir / "nograph.yaml").write_text(
        "stores:\n  neo4j:\n    uri: bolt://neo4j:7687\n    database: neo4j\n"
        "    store_chunk_text: false\n    max_connection_pool_size: 20\n"
        "    connection_timeout_s: 10\n    query_timeout_s: 15\n"
    )
    monkeypatch.setenv("APP_ENV", "nograph")
    with pytest.raises(ValidationError, match="allow_graph_only"):
        Settings()


def test_resolution_thresholds_ordered(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = make_isolated_config_dir(tmp_path, monkeypatch)
    (config_dir / "badthresh.yaml").write_text(
        "resolution:\n  auto_reject_threshold: 0.95\n  auto_merge_threshold: 0.90\n"
    )
    monkeypatch.setenv("APP_ENV", "badthresh")
    with pytest.raises(ValidationError):
        Settings()


def test_scorer_weights_sum_to_one(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = make_isolated_config_dir(tmp_path, monkeypatch)
    (config_dir / "badweights.yaml").write_text(
        "resolution:\n  scorer_weights:\n    jaro_winkler: 0.5\n"
        "    token_set_ratio: 0.5\n    embedding_cosine: 0.5\n"
    )
    monkeypatch.setenv("APP_ENV", "badweights")
    with pytest.raises(ValidationError, match=r"sum to 1\.0"):
        Settings()


def test_chunk_overlap_less_than_size(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = make_isolated_config_dir(tmp_path, monkeypatch)
    (config_dir / "badchunk.yaml").write_text(
        "ingestion:\n  chunk_size: 100\n  chunk_overlap: 150\n"
    )
    monkeypatch.setenv("APP_ENV", "badchunk")
    with pytest.raises(ValidationError, match="chunk_overlap"):
        Settings()


def test_sparse_enabled_requires_idf_modifier(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = make_isolated_config_dir(tmp_path, monkeypatch)
    (config_dir / "badsparse.yaml").write_text(
        "embedding:\n  sparse:\n    model: Qdrant/bm25\n    enabled: true\n"
        "    idf_modifier: false\n"
    )
    monkeypatch.setenv("APP_ENV", "badsparse")
    with pytest.raises(ValidationError, match="idf_modifier"):
        Settings()


def test_secrets_never_in_repr() -> None:
    settings = Settings()
    for secret_value in [
        "sk-test-master-key",
        "sk-test-virtual-key",
        "sk-test-admin-key",
    ]:
        assert secret_value not in repr(settings)
        assert secret_value not in str(settings)
        assert secret_value not in settings.model_dump_json()
    assert "sk-" not in repr(settings)
    assert "sk-" not in settings.model_dump_json()


def test_config_hash_stable_and_secret_free(monkeypatch: pytest.MonkeyPatch) -> None:
    first = Settings().config_hash
    second = Settings().config_hash
    assert first == second

    monkeypatch.setenv("GRAPHRAG_SECRETS__LITELLM_MASTER_KEY", "sk-a-totally-different-key")
    third = Settings().config_hash
    assert third == first
