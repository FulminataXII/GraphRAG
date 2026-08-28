from __future__ import annotations

import ast
from pathlib import Path

import pytest

import graphrag.core.ports as ports_module
from graphrag.core.ports import (
    Cache,
    Clock,
    DocumentLedger,
    Embedder,
    GraphStore,
    IdGenerator,
    JobQueue,
    LLMClient,
    SourceRegistry,
    VectorStore,
)
from tests.fakes import (
    FakeCache,
    FakeClock,
    FakeDocumentLedger,
    FakeEmbedder,
    FakeGraphStore,
    FakeIdGenerator,
    FakeJobQueue,
    FakeLLMClient,
    FakeSourceRegistry,
    FakeVectorStore,
)

_FAKE_TO_PORT = [
    (FakeClock, Clock),
    (FakeIdGenerator, IdGenerator),
    (FakeEmbedder, Embedder),
    (FakeVectorStore, VectorStore),
    (FakeGraphStore, GraphStore),
    (FakeLLMClient, LLMClient),
    (FakeCache, Cache),
    (FakeJobQueue, JobQueue),
    (FakeSourceRegistry, SourceRegistry),
    (FakeDocumentLedger, DocumentLedger),
]


@pytest.mark.parametrize("fake_cls,port", _FAKE_TO_PORT, ids=[p.__name__ for _, p in _FAKE_TO_PORT])
def test_fakes_satisfy_protocols(fake_cls: type, port: type) -> None:
    """isinstance() against a runtime_checkable Protocol only checks attribute names exist —
    not signatures. That's fully enforced by mypy --strict in CI; this is a smoke check."""
    assert isinstance(fake_cls(), port)


def test_ports_reference_only_core_types() -> None:
    """core/ is the leaf layer: every graphrag.* import in ports.py must be graphrag.core.*."""
    source_path = Path(ports_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("graphrag."):
            top_level = node.module.split(".")[1]
            if top_level != "core":
                offenders.append(node.module)

    assert offenders == [], f"core/ports.py imports outside graphrag.core: {offenders}"
