"""Shared helpers for BO-00 config tests. Not a conftest fixture module on purpose —
tests/conftest.py with fakes/factories is a BO-01 deliverable; these tests are self-contained.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REQUIRED_SECRET_ENV = {
    "GRAPHRAG_SECRETS__POSTGRES_DSN": "postgresql://user:pass@localhost:5432/graphrag",
    "GRAPHRAG_SECRETS__LITELLM_MASTER_KEY": "sk-test-master-key",
    "GRAPHRAG_SECRETS__LITELLM_VIRTUAL_KEY": "sk-test-virtual-key",
    "GRAPHRAG_SECRETS__NEO4J_PASSWORD": "test-neo4j-password",
    "GRAPHRAG_SECRETS__ADMIN_API_KEY": "sk-test-admin-key",
}

REAL_CONFIG_DIR = Path(__file__).resolve().parents[2] / "graphrag" / "config"


def set_required_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_SECRET_ENV.items():
        monkeypatch.setenv(key, value)


def make_isolated_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copy the real base.yaml into an isolated tmp dir and point Settings at it.

    Lets a test add its own override file (e.g. `local.yaml`, or a deliberately broken one)
    under APP_ENV without touching the real, checked-in config files.
    """
    import graphrag.config.settings as settings_module

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copy(REAL_CONFIG_DIR / "base.yaml", config_dir / "base.yaml")
    monkeypatch.setattr(settings_module, "_CONFIG_DIR", config_dir)
    return config_dir
