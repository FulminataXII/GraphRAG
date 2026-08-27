from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_check_layering():
    spec = importlib.util.spec_from_file_location(
        "check_layering", REPO_ROOT / "scripts" / "check_layering.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_layering = _load_check_layering()


def test_layering_script_catches_violation(tmp_path: Path) -> None:
    bad_file = tmp_path / "resolve.py"
    bad_file.write_text("from graphrag.adapters.qdrant_store import QdrantStore\n")

    violations = check_layering.check_file(bad_file, "services")
    assert len(violations) == 1
    assert "adapters" in str(violations[0])


def test_layering_script_allows_permitted_import(tmp_path: Path) -> None:
    good_file = tmp_path / "resolve.py"
    good_file.write_text("from graphrag.core.models import Chunk\n")

    violations = check_layering.check_file(good_file, "services")
    assert violations == []


def test_layering_script_passes_on_real_tree() -> None:
    assert check_layering.main() == 0
