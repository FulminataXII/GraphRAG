"""CI guard enforcing the import table in BLUEPRINT.md §0.

Contract:
    - Walks every .py file under graphrag/<layer>/.
    - Parses imports with ast (no execution, so it's safe on partially-written code).
    - A `graphrag.<other_layer>` import that isn't in the allowed set for the current
      layer is a violation.
    - Exits 1 and prints every violation (file, line, offending import) if any are found.
    - Exits 0 silently otherwise.

Layer | May import from graphrag.*
    core      -> core only
    services  -> core, services
    adapters  -> core, config, adapters
    apps      -> everything (core, config, services, adapters, apps)
    config    -> config only (no graphrag.* at all, per BLUEPRINT: "any graphrag.*" forbidden)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "graphrag"

# Layer -> set of graphrag top-level packages it may import from (besides itself).
ALLOWED: dict[str, set[str]] = {
    "core": set(),
    "services": {"core"},
    "adapters": {"core", "config"},
    "apps": {"core", "config", "services", "adapters"},
    "config": set(),
}


class Violation:
    def __init__(self, file: Path, lineno: int, module: str) -> None:
        self.file = file
        self.lineno = lineno
        self.module = module

    def __str__(self) -> str:
        try:
            rel = self.file.relative_to(REPO_ROOT)
        except ValueError:
            rel = self.file
        return f"{rel}:{self.lineno}: illegal import of `{self.module}`"


def _imported_modules(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module]
    return []


def _layer_of(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "graphrag":
        return parts[1]
    return None


def check_file(path: Path, layer: str) -> list[Violation]:
    violations: list[Violation] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    allowed = ALLOWED[layer] | {layer}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        for module in _imported_modules(node):
            imported_layer = _layer_of(module)
            if imported_layer is None:
                continue
            if imported_layer not in allowed:
                violations.append(Violation(path, node.lineno, module))
    return violations


def main() -> int:
    violations: list[Violation] = []
    for layer in ALLOWED:
        layer_dir = PACKAGE_ROOT / layer
        if not layer_dir.is_dir():
            continue
        for path in sorted(layer_dir.rglob("*.py")):
            violations.extend(check_file(path, layer))

    if violations:
        print("Layering violations found:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1

    print("Layering OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
