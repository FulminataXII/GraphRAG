"""`WorkerSettings`/`ProjectionWorkerSettings` unit tests. See BLUEPRINT §7.2 / BO-05.

`apps/worker/settings.py` reads `get_settings()` at import (class-body) time, so this module
must set up config/secrets BEFORE importing it — same as `tests.conftest`'s `settings` fixture
does for everything else.
"""

from __future__ import annotations

import pytest

from tests.unit._settings_helpers import REQUIRED_SECRET_ENV


@pytest.fixture
def worker_settings_module(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_ENV", "test")
    for key, value in REQUIRED_SECRET_ENV.items():
        monkeypatch.setenv(key, value)
    import graphrag.apps.worker.settings as module

    return module


def test_projection_worker_is_single_concurrency(worker_settings_module) -> None:
    """`ProjectionWorkerSettings.max_jobs == 1` — this single value is what removes the Qdrant
    sources[] read-modify-write race (BLUEPRINT §7.2). Any value > 1 reintroduces it."""
    assert worker_settings_module.ProjectionWorkerSettings.max_jobs == 1


def test_projection_worker_queue_name_from_config(worker_settings_module) -> None:
    assert worker_settings_module.ProjectionWorkerSettings.queue_name == "projection"


def test_worker_settings_functions_registered(worker_settings_module) -> None:
    names = {fn.__name__ for fn in worker_settings_module.WorkerSettings.functions}
    assert names == {"ingest_document", "delete_document"}


def test_worker_settings_max_tries_from_dead_letter_config(worker_settings_module) -> None:
    assert worker_settings_module.WorkerSettings.max_tries == 3
    assert worker_settings_module.WorkerSettings.retry_jobs is True
