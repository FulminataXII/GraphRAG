"""Shared test isolation. Not the full BO-01 conftest (fakes/factories/container fixtures
land there) — just enough so no test can read the repo's real `.env` or ambient shell env.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_cwd_and_env(
    request: pytest.FixtureRequest, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chdir to an empty tmp_path and clear every GRAPHRAG_* env var before each unit test.

    Settings() resolves its `.env` relative to the process cwd and reads GRAPHRAG_*-prefixed
    env vars directly — without this, a unit test could silently pass (or fail) depending on
    whatever real secrets/overrides happen to be sitting in the repo's `.env` or the
    developer's shell. A test that deliberately wants a `.env` writes one into tmp_path itself.

    Integration tests are exempt: they exercise the real stack via `docker compose`, which
    must run from the repo root (to find docker-compose.yml) against the real `.env` (which
    Compose itself reads for interpolation) — isolating them here would just break `make up`.
    """
    if "integration" in request.node.keywords:
        return
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("GRAPHRAG_"):
            monkeypatch.delenv(key, raising=False)
