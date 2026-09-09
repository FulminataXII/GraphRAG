"""Golden set loader. See BLUEPRINT §8.

Loads and validates all `*.yaml` files from the golden set directory into a list of
`GoldenItem` instances. Each item represents one evaluation question with its expected
route, chunk_ids, answer, and refusal status.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict


class GoldenItem(BaseModel):
    """One evaluation question with ground truth. See BLUEPRINT §8."""

    model_config = ConfigDict(frozen=True)

    id: str
    question: str
    category: Literal["single_hop", "multi_hop", "thematic", "unanswerable"]
    gold_route: Literal["vector", "graph", "hybrid"]
    gold_chunk_ids: list[UUID]
    gold_answer: str | None
    must_refuse: bool = False


def load_golden_set(path: str | Path) -> list[GoldenItem]:
    """Load all ``*.yaml`` files from *path* and return validated ``GoldenItem`` instances.

    Files are sorted by name for deterministic ordering.  Each YAML file must contain a
    top-level list of item mappings.

    Raises:
        FileNotFoundError: if *path* does not exist.
        ValueError: if no YAML files are found or a file contains invalid structure.
        pydantic.ValidationError: if an item fails schema validation.
    """
    directory = Path(path)
    if not directory.is_dir():
        raise FileNotFoundError(f"golden set directory does not exist: {directory}")

    yaml_files = sorted(directory.glob("*.yaml"))
    if not yaml_files:
        raise ValueError(f"no YAML files found in {directory}")

    items: list[GoldenItem] = []
    for yaml_path in yaml_files:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"{yaml_path} must contain a top-level list, got {type(raw).__name__}")
        for entry in raw:
            # Normalise empty gold_chunk_ids from YAML null to an empty list
            if entry.get("gold_chunk_ids") is None:
                entry["gold_chunk_ids"] = []
            items.append(GoldenItem.model_validate(entry))

    return items
