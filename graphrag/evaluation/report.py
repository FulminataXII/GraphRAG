"""Eval report model and markdown renderer. See BLUEPRINT §8."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class EvalReport(BaseModel):
    """Aggregated evaluation results for one run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    git_sha: str
    config_hash: str
    started_at: datetime
    finished_at: datetime
    metrics: dict[str, float]
    per_item_results: list[dict[str, Any]]
    models_served: list[str]
    mixed_judges: bool
    thresholds_passed: bool
    threshold_details: dict[str, dict[str, Any]]
    category_breakdown: dict[str, dict[str, float]]


def render_markdown(report: EvalReport) -> str:
    """Metric table with pass/fail against thresholds, per-category breakdown, and the
    refusal rate on unanswerable items called out separately."""
    lines: list[str] = []
    lines.append("# Evaluation Report")
    lines.append("")
    lines.append(f"- **Run ID:** {report.run_id}")
    lines.append(f"- **Git SHA:** {report.git_sha}")
    lines.append(f"- **Config Hash:** {report.config_hash}")
    lines.append(f"- **Started:** {report.started_at.isoformat()}")
    lines.append(f"- **Finished:** {report.finished_at.isoformat()}")
    lines.append(f"- **Items:** {len(report.per_item_results)}")
    lines.append(f"- **Pass:** {'✅' if report.thresholds_passed else '❌'}")

    if report.mixed_judges:
        lines.append("")
        lines.append("> ⚠️ **Mixed judges detected.** Multiple models served during this run.")
    lines.append("")
    lines.append(f"**Models served:** {', '.join(sorted(set(report.models_served))) or 'none'}")

    # --- Metric summary table ---
    lines.append("")
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Score | Threshold | Pass |")
    lines.append("|--------|-------|-----------|------|")
    for metric_name, detail in sorted(report.threshold_details.items()):
        score = detail.get("score", 0.0)
        threshold = detail.get("threshold")
        passed = detail.get("passed", True)
        threshold_str = f"{threshold:.2f}" if threshold is not None else "—"
        status = "✅" if passed else "❌"
        lines.append(f"| {metric_name} | {score:.4f} | {threshold_str} | {status} |")

    # Additional metrics not in thresholds
    threshold_names = set(report.threshold_details)
    for metric_name, score in sorted(report.metrics.items()):
        if metric_name not in threshold_names:
            lines.append(f"| {metric_name} | {score:.4f} | — | — |")

    # --- Per-category breakdown ---
    if report.category_breakdown:
        lines.append("")
        lines.append("## Per-Category Breakdown")
        lines.append("")
        categories = sorted(report.category_breakdown)
        metric_names = sorted(
            {m for cat_metrics in report.category_breakdown.values() for m in cat_metrics}
        )
        header = "| Category | " + " | ".join(metric_names) + " |"
        sep = "|----------|" + "|".join("-" * (len(m) + 2) for m in metric_names) + "|"
        lines.append(header)
        lines.append(sep)
        for cat in categories:
            cat_metrics = report.category_breakdown[cat]
            values = " | ".join(f"{cat_metrics.get(m, 0.0):.4f}" for m in metric_names)
            lines.append(f"| {cat} | {values} |")

    # --- Refusal rate ---
    unanswerable_items = [
        item for item in report.per_item_results if item.get("category") == "unanswerable"
    ]
    if unanswerable_items:
        refused = sum(1 for item in unanswerable_items if item.get("refused", False))
        rate = refused / len(unanswerable_items) if unanswerable_items else 0.0
        lines.append("")
        lines.append("## Unanswerable Items")
        lines.append("")
        lines.append(f"Refusal rate: **{refused}/{len(unanswerable_items)}** ({rate:.1%})")

    lines.append("")
    return "\n".join(lines)
