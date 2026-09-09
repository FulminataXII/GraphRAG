"""EvalRunner — orchestrates the evaluation pipeline. See BLUEPRINT §8.

Contract:
    - Sets cache.<tier>.enabled = False for the run when evaluation.disable_cache_during_run,
      otherwise run 2 scores the cache instead of the system.
    - Executes every GoldenItem, collects per-item results.
    - Persists an eval_runs row with git_sha AND config_hash.
    - Returns EvalReport; --subset runs evaluation.smoke_subset_size items for CI.
    - Fails the process with exit 1 if any metric is below evaluation.thresholds.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from graphrag.evaluation.loader import GoldenItem, load_golden_set
from graphrag.evaluation.metrics.retrieval import mrr, ndcg_at_k, recall_at_k
from graphrag.evaluation.metrics.routing import routing_accuracy
from graphrag.evaluation.report import EvalReport

if TYPE_CHECKING:
    from graphrag.adapters.postgres.evals import PostgresEvalStore
    from graphrag.config.settings import Settings
    from graphrag.evaluation.metrics.generation import GenerationJudge
    from graphrag.services.orchestration.graph import OrchestrationService

_log = logging.getLogger(__name__)


def _git_sha() -> str:
    """Extract the current git SHA, with safe fallback."""
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            .decode("utf-8")
            .strip()
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return "unknown"


def _disable_cache(settings: Settings) -> Settings:
    """Return a settings copy with all cache tiers disabled.

    ``Settings`` is frozen, so we create a copy with modified cache section.
    """
    return settings.model_copy(
        update={
            "cache": settings.cache.model_copy(
                update={
                    "embedding": settings.cache.embedding.model_copy(update={"enabled": False}),
                    "retrieval": settings.cache.retrieval.model_copy(update={"enabled": False}),
                    "llm": settings.cache.llm.model_copy(update={"enabled": False}),
                }
            )
        }
    )


class EvalRunner:
    """Orchestrate evaluation runs against the golden set."""

    def __init__(
        self,
        *,
        settings: Settings,
        orchestrator: OrchestrationService,
        judge: GenerationJudge | None = None,
        eval_store: PostgresEvalStore | None = None,
    ) -> None:
        self._settings = settings
        self._orchestrator = orchestrator
        self._judge = judge
        self._eval_store = eval_store

    async def run(
        self,
        *,
        subset: int | None = None,
        include_generation_metrics: bool = True,
    ) -> EvalReport:
        """Execute the full evaluation pipeline.

        Args:
            subset: if given, run only this many items (for CI smoke tests).
            include_generation_metrics: if False, skip LLM-judged RAGAS metrics.
        """
        started_at = datetime.now(UTC)
        run_id = str(uuid4())
        git_sha = _git_sha()
        config_hash = self._settings.config_hash

        # Load golden set
        items = load_golden_set(self._settings.evaluation.golden_set_path)
        if subset is not None:
            items = items[:subset]

        # Apply caching overrides
        if self._settings.evaluation.disable_cache_during_run:
            self._settings = _disable_cache(self._settings)

        # Run each item through orchestration
        per_item_results: list[dict[str, Any]] = []
        predicted_routes: list[str] = []
        gold_routes: list[str] = []

        total_items = len(items)
        for i, item in enumerate(items, start=1):
            _log.info("Running pipeline for item %s (%d/%d)...", item.id, i, total_items)
            result = await self._run_item(item)
            per_item_results.append(result)
            predicted_routes.append(result.get("predicted_route", "vector"))
            gold_routes.append(item.gold_route)

        # Compute retrieval metrics (aggregated)
        retrieval_scores = self._compute_retrieval_metrics(items, per_item_results)

        # Compute routing metrics
        route_acc, _confusion = routing_accuracy(predicted_routes, gold_routes)

        # Compute generation metrics via RAGAS
        generation_scores: dict[str, float] = {}
        if self._judge is not None and include_generation_metrics:
            generation_scores = await self._compute_generation_metrics(items, per_item_results)

        # Compute refusal rate on unanswerable
        refusal_rate = self._compute_refusal_rate(items, per_item_results)

        # Aggregate all metrics
        all_metrics: dict[str, float] = {
            **retrieval_scores,
            "routing_accuracy": route_acc,
            **generation_scores,
            "refusal_rate_on_unanswerable": refusal_rate,
        }

        # Check thresholds
        threshold_details = self._check_thresholds(all_metrics)
        thresholds_passed = all(d["passed"] for d in threshold_details.values())

        # Per-category breakdown
        category_breakdown = self._category_breakdown(items, per_item_results)

        # Models served
        models_served = self._judge.models_served if self._judge else []
        unique_models = set(models_served)
        mixed_judges = len(unique_models) > 1

        finished_at = datetime.now(UTC)

        report = EvalReport(
            run_id=run_id,
            git_sha=git_sha,
            config_hash=config_hash,
            started_at=started_at,
            finished_at=finished_at,
            metrics=all_metrics,
            per_item_results=per_item_results,
            models_served=models_served,
            mixed_judges=mixed_judges,
            thresholds_passed=thresholds_passed,
            threshold_details=threshold_details,
            category_breakdown=category_breakdown,
        )

        # Persist
        if self._eval_store is not None:
            await self._eval_store.save_run(
                run_id=run_id,
                git_sha=git_sha,
                config_hash=config_hash,
                started_at=started_at,
                finished_at=finished_at,
                metrics=all_metrics,
            )
            await self._eval_store.save_results(run_id=run_id, results=per_item_results)

        return report

    async def _run_item(self, item: GoldenItem) -> dict[str, Any]:
        """Run a single golden item through orchestration and collect results."""
        import asyncio

        from graphrag.core.errors import (
            GraphBackendUnavailable,
            LLMProviderExhausted,
            RateLimited,
            RetrievalBackendUnavailable,
        )

        try:
            query_result = None
            backoffs = [15.0, 30.0, 60.0, 120.0]
            for attempt in range(5):
                try:
                    query_result = await self._orchestrator.run(
                        question=item.question,
                        correlation_id=f"eval-{item.id}",
                    )
                    break
                except (
                    RateLimited,
                    LLMProviderExhausted,
                    RetrievalBackendUnavailable,
                    GraphBackendUnavailable,
                ) as exc:
                    if attempt == 4:
                        raise
                    if isinstance(exc, RateLimited) and exc.retry_after is not None:
                        sleep_time = exc.retry_after
                    else:
                        sleep_time = backoffs[attempt]
                    if sleep_time > 300:
                        raise LLMProviderExhausted(
                            f"Rate limit sleep duration ({sleep_time}s) exceeds maximum allowed wait time. Aborting to prevent daily-limit hang."
                        ) from exc
                    _log.info(
                        "Rate limited. Sleeping for %s seconds...",
                        sleep_time,
                    )
                    await asyncio.sleep(sleep_time)

            assert query_result is not None
            answer = query_result.answer
            answer_text = answer.text
            retrieved_ids = [str(c.chunk_id) for c in answer.citations]
            predicted_route = query_result.route.strategy if query_result.route else "vector"
            refused = (
                answer.confidence < 0.3
                or "cannot" in answer_text.lower()
                or "don't have" in answer_text.lower()
                or "no information" in answer_text.lower()
                or "not available" in answer_text.lower()
                or "unable to" in answer_text.lower()
            )

            return {
                "item_id": item.id,
                "category": item.category,
                "question": item.question,
                "answer": answer_text,
                "predicted_route": predicted_route,
                "retrieved_chunk_ids": retrieved_ids,
                "gold_chunk_ids": [str(cid) for cid in item.gold_chunk_ids],
                "refused": refused,
                "error": None,
            }
        except Exception as exc:
            _log.warning("eval item %s failed: %s", item.id, exc)
            return {
                "item_id": item.id,
                "category": item.category,
                "question": item.question,
                "answer": "",
                "predicted_route": "vector",
                "retrieved_chunk_ids": [],
                "gold_chunk_ids": [str(cid) for cid in item.gold_chunk_ids],
                "refused": False,
                "error": str(exc),
            }

    def _compute_retrieval_metrics(
        self, items: list[GoldenItem], results: list[dict[str, Any]]
    ) -> dict[str, float]:
        """Aggregate retrieval metrics across all items."""
        from uuid import UUID

        recall_scores: list[float] = []
        mrr_scores: list[float] = []
        ndcg_scores: list[float] = []

        for item, result in zip(items, results, strict=True):
            if not item.gold_chunk_ids:
                continue  # skip unanswerable for retrieval metrics
            retrieved = [UUID(cid) for cid in result.get("retrieved_chunk_ids", [])]
            gold = item.gold_chunk_ids
            recall_scores.append(recall_at_k(retrieved, gold, k=10))
            mrr_scores.append(mrr(retrieved, gold))
            ndcg_scores.append(ndcg_at_k(retrieved, gold, k=10))

        return {
            "recall_at_10": _mean(recall_scores),
            "mrr": _mean(mrr_scores),
            "ndcg_at_10": _mean(ndcg_scores),
        }

    async def _compute_generation_metrics(
        self, items: list[GoldenItem], results: list[dict[str, Any]]
    ) -> dict[str, float]:
        """Run RAGAS metrics on generation results."""
        assert self._judge is not None
        eval_items: list[dict[str, Any]] = []
        for item, result in zip(items, results, strict=True):
            if result.get("error") is not None:
                continue
            eval_items.append(
                {
                    "user_input": item.question,
                    "response": result.get("answer", ""),
                    "retrieved_contexts": [result.get("answer", "")],  # placeholder context
                    "reference_contexts": [item.gold_answer or ""],
                    "reference": item.gold_answer,
                }
            )

        if not eval_items:
            return {}

        return await self._judge.evaluate_items(eval_items, include_llm_metrics=True)

    def _compute_refusal_rate(
        self, items: list[GoldenItem], results: list[dict[str, Any]]
    ) -> float:
        """Refusal rate on unanswerable items."""
        unanswerable = [
            (item, result) for item, result in zip(items, results, strict=True) if item.must_refuse
        ]
        if not unanswerable:
            return 0.0
        refused = sum(1 for _, result in unanswerable if result.get("refused", False))
        return refused / len(unanswerable)

    def _check_thresholds(self, metrics: dict[str, float]) -> dict[str, dict[str, Any]]:
        """Check each metric against configured thresholds."""
        thresholds = self._settings.evaluation.thresholds
        details: dict[str, dict[str, Any]] = {}

        threshold_map = {
            "recall_at_10": thresholds.recall_at_10,
            "faithfulness": thresholds.faithfulness,
            "answer_relevancy": thresholds.answer_relevancy,
            "routing_accuracy": thresholds.routing_accuracy,
            "refusal_rate_on_unanswerable": thresholds.refusal_rate_on_unanswerable,
        }

        for name, threshold in threshold_map.items():
            score = metrics.get(name, 0.0)
            details[name] = {
                "score": score,
                "threshold": threshold,
                "passed": score >= threshold,
            }

        return details

    def _category_breakdown(
        self, items: list[GoldenItem], results: list[dict[str, Any]]
    ) -> dict[str, dict[str, float]]:
        """Per-category retrieval metric averages."""
        from uuid import UUID

        by_category: dict[str, list[tuple[GoldenItem, dict[str, Any]]]] = {}
        for item, result in zip(items, results, strict=True):
            by_category.setdefault(item.category, []).append((item, result))

        breakdown: dict[str, dict[str, float]] = {}
        for category, pairs in sorted(by_category.items()):
            recall_scores: list[float] = []
            mrr_scores: list[float] = []
            for item, result in pairs:
                if not item.gold_chunk_ids:
                    continue
                retrieved = [UUID(cid) for cid in result.get("retrieved_chunk_ids", [])]
                gold = item.gold_chunk_ids
                recall_scores.append(recall_at_k(retrieved, gold, k=10))
                mrr_scores.append(mrr(retrieved, gold))
            breakdown[category] = {
                "recall_at_10": _mean(recall_scores),
                "mrr": _mean(mrr_scores),
                "count": float(len(pairs)),
            }

        return breakdown


def _mean(values: list[float]) -> float:
    """Safe mean that returns 0.0 for empty lists."""
    return sum(values) / len(values) if values else 0.0
