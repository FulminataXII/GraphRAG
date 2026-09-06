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
    configured_llm_roles,
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


def test_script_structured_rejects_a_role_no_config_defines() -> None:
    """The guard that keeps a scripted queue from being silently unreachable.

    A queue keyed by a NODE name rather than an LLM role is never consumed: the node asks for
    its role, finds nothing, raises `LLMProviderExhausted`, and because so much of this pipeline
    fails open the test still goes green. `test_each_node_is_pure` scripted five node names that
    way and asserted nothing for several build orders. Queue-time is the right place to catch
    it — the bad name is still in hand there.
    """
    client = FakeLLMClient()

    for node_name in (
        "plan_route",
        "grade_context",
        "rewrite_query",
        "generate",
        "verify_grounded",
    ):
        with pytest.raises(ValueError, match=f"unknown LLM role '{node_name}'"):
            client.script_structured(node_name, object())

    # Nothing was queued by a rejected call, so a later correct one is unaffected.
    assert client._queues == {}


def test_script_structured_accepts_every_configured_role() -> None:
    """The other direction: the guard must not reject a role the system really uses, or it
    becomes a reason to delete the guard. Derived from config, so adding a role to
    `llm.roles` keeps this passing with no edit here."""
    client = FakeLLMClient()
    roles = configured_llm_roles()

    assert {"router", "grader", "bulk", "synth", "judge"} <= roles
    for role in roles:
        client.script_structured(role, object())

    assert set(client._queues) == roles


def test_configured_roles_come_from_config_not_a_literal() -> None:
    """`configured_llm_roles` must track `config/base.yaml`. If this list were hardcoded in
    `tests/fakes.py`, a renamed role would make the guard reject the real name."""
    import yaml

    import graphrag.config

    # Located from the package rather than `settings._CONFIG_DIR`: that name is private, and
    # exporting it to satisfy a test would be the test changing production code to suit itself.
    base_yaml = Path(graphrag.config.__file__).resolve().parent / "base.yaml"
    declared = yaml.safe_load(base_yaml.read_text(encoding="utf-8"))
    assert configured_llm_roles() == frozenset(declared["llm"]["roles"])
