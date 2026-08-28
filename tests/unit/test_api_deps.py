"""`apps/api/deps.py` unit tests. See BLUEPRINT §7.1."""

from __future__ import annotations

import inspect

import pytest
from starlette.requests import Request

from graphrag.apps.api.deps import get_container


def test_get_container_reads_app_state() -> None:
    scope = {"type": "http", "app": _FakeApp(container="sentinel-container")}
    request = Request(scope)
    assert get_container(request) == "sentinel-container"


def test_container_requires_request_in_dependency() -> None:
    """`get_container`'s only parameter is a required `Request` — calling it without one is a
    TypeError, which is what makes FastAPI's dependency injection (which supplies Request
    automatically) the only way to satisfy it."""
    signature = inspect.signature(get_container)
    (param,) = signature.parameters.values()
    assert param.name == "request"
    assert param.default is inspect.Parameter.empty

    with pytest.raises(TypeError):
        get_container()  # type: ignore[call-arg]


class _FakeApp:
    def __init__(self, container: str) -> None:
        self.state = _FakeState(container)


class _FakeState:
    def __init__(self, container: str) -> None:
        self.container = container
