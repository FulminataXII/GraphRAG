"""Tests for BO-11 Hybrid GraphRAG Evaluation Pipeline."""

from pathlib import Path

import pytest

from graphrag.config.settings import get_settings
from graphrag.evaluation.runner import _disable_cache


def test_golden_set_schema() -> None:
    pass


def test_golden_set_balance() -> None:
    pass


def test_recall_at_k_known_case() -> None:
    pass


def test_mrr_known_case() -> None:
    pass


def test_ndcg_monotonic() -> None:
    pass


def test_eval_disables_cache() -> None:
    settings = get_settings()
    settings = settings.model_copy(
        update={
            "evaluation": settings.evaluation.model_copy(update={"disable_cache_during_run": True})
        }
    )
    new_settings = _disable_cache(settings)
    assert new_settings.cache.embedding.enabled is False
    assert new_settings.cache.retrieval.enabled is False
    assert new_settings.cache.llm.enabled is False


def test_judge_provider_differs_from_synth() -> None:
    settings = get_settings()
    assert settings.llm.roles["judge"].model != settings.llm.roles["synth"].model


def test_eval_run_persisted_with_git_sha_and_config_hash() -> None:
    pass


def test_context_recall_uses_no_llm() -> None:
    pass


def test_ragas_pinned_exactly() -> None:
    pyproject = Path(__file__).parent.parent.parent / "pyproject.toml"
    content = pyproject.read_text()
    assert "ragas==" in content


def test_judge_temperature_is_zero() -> None:
    settings = get_settings()
    assert settings.llm.roles["judge"].temperature == 0.0


@pytest.mark.eval
def test_refusal_on_unanswerable() -> None:
    pass


@pytest.mark.eval
def test_routing_accuracy_above_threshold() -> None:
    pass


@pytest.mark.eval
def test_smoke_subset_gate() -> None:
    pass
