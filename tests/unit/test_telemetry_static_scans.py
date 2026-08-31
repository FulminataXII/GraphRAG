"""BO-02 static-scan tests. See BUILD_ORDER.md.

Both scans parse source with `ast` rather than importing — importing would eagerly pull in
`opentelemetry.sdk._logs` from wherever it's first imported, which is exactly what the first
scan is checking for, and doing that via `import` rather than `ast` would make the test's own
import order part of the result.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from graphrag.adapters.telemetry.metrics import INSTRUMENT_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
GRAPHRAG_ROOT = REPO_ROOT / "graphrag"
TELEMETRY_LOGGING_PATH = GRAPHRAG_ROOT / "adapters" / "telemetry" / "logging.py"
METRICS_PATH = GRAPHRAG_ROOT / "adapters" / "telemetry" / "metrics.py"

# Instruments whose emitting component isn't built yet.
# Remove an entry when its emitter lands — test_every_declared_metric_has_an_emitter fails
# until you do, so the allowlist can't quietly become permanent.
PENDING_INSTRUMENTS: frozenset[str] = frozenset(
    {
        "route_selected",  # BO-10: plan_route node
        "retrieval_latency",  # BO-09: VectorRetriever/GraphRetriever.retrieve
        "answer_refused",  # BO-10: insufficient node
        "citations_invalid",  # BO-10: verify_citations node
        "grader_degraded",  # BO-10: grade_context node
        "projection_lag",  # ProjectionService has no Clock/timestamp wiring yet
    }
)


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


class _EmissionScanner(ast.NodeVisitor):
    """Finds real `<metrics-like>.<instrument>.add(...)` / `.record(...)` call sites in one file.

    Matching call *shape* (two-level attribute access ending in `.add`/`.record`, base name
    ending in "metrics") rather than raw text keeps this from tripping over unrelated matches —
    `repair_attempts` alone shows up as a Pydantic field, an OTel span attribute
    (`llm.repair_attempts`), and the real metric call, three unrelated hits for one string.

    A "site" is `(file, enclosing function)`, not one entry per call: a single instrument may
    legitimately be emitted by several adjacent calls in the same function distinguished only by
    label attributes — see `llm_tokens`'s prompt/completion split in `litellm_client.py`. That's
    one emission site, not two.
    """

    def __init__(self, path: Path) -> None:
        self._path = path.relative_to(REPO_ROOT)
        self._func_stack: list[str] = []
        self.sites: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self.stray: list[str] = []

    def _enclosing(self) -> str:
        return ".".join(self._func_stack) if self._func_stack else "<module>"

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    @staticmethod
    def _base_identifier(node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in ("add", "record")
            and isinstance(func.value, ast.Attribute)
        ):
            base = self._base_identifier(func.value.value)
            if base is not None and base.lower().endswith("metrics"):
                name = func.value.attr
                if name in INSTRUMENT_NAMES:
                    self.sites[name].add((str(self._path), self._enclosing()))
                else:
                    self.stray.append(
                        f"{self._path}:{node.lineno}: .{name}.{func.attr}(...) — "
                        f"{name!r} is not a declared Metrics instrument"
                    )
        self.generic_visit(node)


def _scan_emission_sites() -> tuple[dict[str, set[tuple[str, str]]], list[str]]:
    sites: dict[str, set[tuple[str, str]]] = defaultdict(set)
    stray: list[str] = []
    for path in sorted(GRAPHRAG_ROOT.rglob("*.py")):
        if path == METRICS_PATH:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        scanner = _EmissionScanner(path)
        scanner.visit(tree)
        for name, site_set in scanner.sites.items():
            sites[name] |= site_set
        stray.extend(scanner.stray)
    return sites, stray


def test_no_stray_metric_emissions() -> None:
    """Enforced at every build order: every real emission call site names a declared
    instrument, and no wired instrument is emitted from more than one site.

    Catches typos (a misspelled instrument name silently creates a disconnected metric stream)
    and double-counting (the same instrument incremented from two call sites).
    """
    sites, stray = _scan_emission_sites()

    assert not stray, "emission call(s) reference an undeclared instrument:\n" + "\n".join(stray)

    duplicated = {name: sorted(where) for name, where in sites.items() if len(where) > 1}
    assert not duplicated, f"instrument(s) emitted from more than one site: {duplicated}"

    # Sanity check the scanner itself matches a real call site, precisely — not the three
    # unrelated hits a raw substring scan would find for this same name.
    assert sites.get("repair_attempts") == {("graphrag/adapters/litellm_client.py", "structured")}


def test_every_declared_metric_has_an_emitter() -> None:
    """Every declared instrument either has a real emission site or is in PENDING_INSTRUMENTS.

    Checked both ways, so the allowlist can't drift stale in either direction: an instrument
    with no site that isn't marked pending is a dead metric nobody flagged as future work, and
    an instrument marked pending that already has a site means its emitter landed and the entry
    should have been deleted.
    """
    sites, _ = _scan_emission_sites()

    assert set(INSTRUMENT_NAMES) >= PENDING_INSTRUMENTS, (
        "PENDING_INSTRUMENTS references unknown instrument(s): "
        f"{PENDING_INSTRUMENTS - set(INSTRUMENT_NAMES)}"
    )

    dead = {
        name for name in INSTRUMENT_NAMES if name not in PENDING_INSTRUMENTS and not sites.get(name)
    }
    assert not dead, (
        "instrument(s) declared but never emitted, and not marked pending in "
        f"PENDING_INSTRUMENTS: {dead}"
    )

    stale_pending = {name for name in PENDING_INSTRUMENTS if sites.get(name)}
    assert not stale_pending, (
        "instrument(s) in PENDING_INSTRUMENTS already have an emitter — remove from the "
        f"allowlist: {stale_pending}"
    )
