"""BO-02 static-scan tests. See BUILD_ORDER.md.

Both scans parse source with `ast` rather than importing — importing would eagerly pull in
`opentelemetry.sdk._logs` from wherever it's first imported, which is exactly what the first
scan is checking for, and doing that via `import` rather than `ast` would make the test's own
import order part of the result.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GRAPHRAG_ROOT = REPO_ROOT / "graphrag"
TELEMETRY_LOGGING_PATH = GRAPHRAG_ROOT / "adapters" / "telemetry" / "logging.py"


def _imported_modules(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [node.module]
    return []


def test_otel_logs_imports_confined_to_one_module() -> None:
    offenders: list[str] = []
    for path in sorted(GRAPHRAG_ROOT.rglob("*.py")):
        if path == TELEMETRY_LOGGING_PATH:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import | ast.ImportFrom):
                continue
            for module in _imported_modules(node):
                if module == "opentelemetry.sdk._logs" or module.startswith(
                    "opentelemetry.sdk._logs."
                ):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: imports {module}")

    assert not offenders, (
        "opentelemetry.sdk._logs must be imported only by "
        + str(TELEMETRY_LOGGING_PATH.relative_to(REPO_ROOT))
        + ", found:\n"
        + "\n".join(offenders)
    )

    # Sanity check the scan itself actually finds the one legitimate import — a scan that
    # never matches anything would pass vacuously forever.
    tree = ast.parse(TELEMETRY_LOGGING_PATH.read_text(encoding="utf-8"))
    found = any(
        module == "opentelemetry.sdk._logs" or module.startswith("opentelemetry.sdk._logs.")
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for module in _imported_modules(node)
    )
    assert found, "expected telemetry/logging.py to import opentelemetry.sdk._logs somewhere"


def test_every_declared_metric_has_an_emitter() -> None:
    """Each `Metrics` instrument name is referenced at most once outside metrics.py.

    Most call sites (plan_route, VectorRetriever, verify_citations, ...) don't exist until
    later build orders — BUILD_ORDER.md §4.5 lists exactly one intended site per instrument,
    but at BO-02 the honest, checkable invariant is "never more than one", which still catches
    the double-emission bug this test exists for and keeps passing as later BOs each add their
    one site.
    """
    from graphrag.adapters.telemetry.metrics import INSTRUMENT_NAMES

    metrics_path = GRAPHRAG_ROOT / "adapters" / "telemetry" / "metrics.py"
    patterns = {name: re.compile(rf"\b{re.escape(name)}\b") for name in INSTRUMENT_NAMES}
    counts = dict.fromkeys(INSTRUMENT_NAMES, 0)

    for path in sorted(GRAPHRAG_ROOT.rglob("*.py")):
        if path == metrics_path:
            continue
        text = path.read_text(encoding="utf-8")
        for name, pattern in patterns.items():
            counts[name] += len(pattern.findall(text))

    violations = {name: count for name, count in counts.items() if count > 1}
    assert not violations, f"metric(s) referenced from more than one site: {violations}"
