"""`WorkerSettings`/`ProjectionWorkerSettings` unit tests. See BLUEPRINT §7.2 / BO-05.

`apps/worker/settings.py` reads `get_settings()` at import (class-body) time, so this module
must set up config/secrets BEFORE importing it — same as `tests.conftest`'s `settings` fixture
does for everything else.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest

from tests.unit._settings_helpers import REQUIRED_SECRET_ENV

_TASKS_PACKAGE = "graphrag.apps.worker.tasks"
_NON_TASK_MODULES = {"__init__", "_common"}


def _arq_task_functions(module: object) -> list[str]:
    """Names of every `async def name(ctx, env)`-shaped function defined IN this module.

    That shape (BLUEPRINT §7.2: "ctx is arq's dict... the envelope arrives as a positional
    argument") is what distinguishes an actual arq task from a private helper (leading
    underscore, already excluded by not being iterated) or a public non-task helper with a
    different signature (e.g. `extract.py`'s `extract_mentions(chunks, *, llm_client, ...)`,
    which is not `(ctx, env)`-shaped and must NOT be mistaken for a second task in that module).
    """
    names = []
    for name, obj in vars(module).items():
        if name.startswith("_") or not inspect.iscoroutinefunction(obj):
            continue
        if obj.__module__ != module.__name__:
            continue  # imported, not defined here (e.g. a re-exported helper)
        params = list(inspect.signature(obj).parameters)
        if params[:2] == ["ctx", "env"]:
            names.append(name)
    return names


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
    assert names == {
        "ingest_document",
        "extract_entities",
        "resolve_entities",
        "delete_document",
    }


def test_worker_settings_max_tries_from_dead_letter_config(worker_settings_module) -> None:
    assert worker_settings_module.WorkerSettings.max_tries == 3
    assert worker_settings_module.WorkerSettings.retry_jobs is True


def test_every_task_module_function_is_registered(worker_settings_module) -> None:
    """BO-08 Step 0 regression guard.

    `project_chunk_payload` (`tasks/project.py`) was, per the task brief, allegedly enqueued by
    `IngestionService` since BO-05 and consumed by nothing — an unregistered arq function is
    dropped with no error the caller sees. Investigation found that claim did NOT hold for this
    repository: `project_chunk_payload` has been correctly named and registered in
    `ProjectionWorkerSettings.functions` (its own dedicated single-concurrency queue/worker,
    `docker-compose.yml`'s `projection-worker` service) since the very first BO-05 commit
    (`c370482`) — `git log -p` on `apps/worker/settings.py`/`tasks/project.py` shows no period
    where the name or the registration were wrong. `test_worker_settings_functions_registered`
    above is exactly the kind of test that would NOT have caught a real version of this bug
    (a hardcoded set naming only the four functions it already knew about); this test is derived
    from the `tasks/` directory instead, so a fifth task file added later — registered in
    EITHER `WorkerSettings` or `ProjectionWorkerSettings` — can't repeat this silently.
    """
    tasks_dir = Path(importlib.import_module(_TASKS_PACKAGE).__file__).parent
    task_module_names = sorted(
        p.stem for p in tasks_dir.glob("*.py") if p.stem not in _NON_TASK_MODULES
    )
    assert task_module_names, "expected at least one task module under apps/worker/tasks/"

    registered = {
        fn.__name__
        for fn in [
            *worker_settings_module.WorkerSettings.functions,
            *worker_settings_module.ProjectionWorkerSettings.functions,
        ]
    }

    for module_name in task_module_names:
        module = importlib.import_module(f"{_TASKS_PACKAGE}.{module_name}")
        task_functions = _arq_task_functions(module)
        assert task_functions, (
            f"{module_name}.py defines no (ctx, env)-shaped arq task function — either it's "
            "misnamed/mis-shaped, or this scan needs updating"
        )
        for fn_name in task_functions:
            assert fn_name in registered, (
                f"{module_name}.{fn_name} is not registered in WorkerSettings.functions or "
                "ProjectionWorkerSettings.functions -- arq would enqueue it and silently drop "
                "every job"
            )
