"""Routing metrics. See BLUEPRINT §8.

Compares predicted strategy routes against gold routes and produces accuracy + a confusion
matrix for diagnostic drill-down.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict


class ConfusionMatrix(BaseModel):
    """Counts per (predicted, actual) pair for strategy routing.

    ``matrix[predicted][actual]`` = count.
    """

    model_config = ConfigDict(frozen=True)

    matrix: dict[str, dict[str, int]]
    labels: list[str]


def routing_accuracy(pred: Sequence[str], gold: Sequence[str]) -> tuple[float, ConfusionMatrix]:
    """Overall accuracy and confusion matrix.

    Args:
        pred: predicted strategy for each item (``"vector"``/``"graph"``/``"hybrid"``).
        gold: gold strategy for each item.

    Returns:
        A (accuracy, confusion_matrix) tuple.  Accuracy is in [0, 1]; returns 0.0 when
        *pred* is empty.
    """
    if len(pred) != len(gold):
        raise ValueError(f"pred and gold must have equal length, got {len(pred)} vs {len(gold)}")
    if not pred:
        return 0.0, ConfusionMatrix(matrix={}, labels=[])

    labels = sorted({*pred, *gold})
    counts: dict[str, dict[str, int]] = {p: dict.fromkeys(labels, 0) for p in labels}
    correct = 0
    for p, g in zip(pred, gold, strict=True):
        counts[p][g] += 1
        if p == g:
            correct += 1

    return correct / len(pred), ConfusionMatrix(matrix=counts, labels=labels)
