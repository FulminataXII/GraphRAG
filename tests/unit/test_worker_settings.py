"""`WorkerSettings`/`ProjectionWorkerSettings` unit tests. See BLUEPRINT §7.2 / BO-05.

`apps/worker/settings.py` reads `get_settings()` at import (class-body) time, so this module
must set up config/secrets BEFORE importing it — same as `tests.conftest`'s `settings` fixture
does for everything else.

These tests assert against the names arq ACTUALLY registers, obtained by building a real
`arq.worker.Worker` from each settings class. That is the whole point of this file, and the
previous version of it did not do that: it read the last dotted segment of each import-string
path and compared THAT to the enqueue names. arq does no such truncation — `func()` registers a
string-registered function under the entire string — so the assertion compared a value the test
computed against a value the test also computed, and passed while every job in the system failed
with `function 'ingest_document' not found`. A test that cannot observe the bug it is named
after is worse than no test: it is a standing claim that the bug cannot happen.

`create_worker` does not connect to Redis (`Worker.__init__` only builds `RedisSettings`; the
connection is made in `main()`), so calling it here is cheap and offline.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest
from arq.worker import create_worker

from graphrag.core.events import TASK_NAMES
from tests.unit._settings_helpers import REQUIRED_SECRET_ENV

_TASKS_PACKAGE = "graphrag.apps.worker.tasks"
_NON_TASK_MODULES = {"__init__", "_common"}
_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "graphrag"

#: Call targets whose FIRST positional argument is a task name. Each must be given one of the
#: `core.events` constants, never a literal — see `test_no_task_name_literals_at_call_sites`.
_TASK_NAME_CALLEES = frozenset({"enqueue", "run_task"})


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


def _registered_names(settings_cls: object) -> set[str]:
    """The task names arq really registers for `settings_cls`.

    Built by constructing arq's own `Worker`, so whatever normalisation arq applies to
    `functions` is applied here too. Nothing in this helper interprets an import path.
    """
    return set(create_worker(settings_cls).functions)  # type: ignore[arg-type]


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
    """The main queue registers exactly its four tasks, under the names callers enqueue with."""
    from graphrag.core.events import (
        DELETE_DOCUMENT,
        EXTRACT_ENTITIES,
        INGEST_DOCUMENT,
        RESOLVE_ENTITIES,
    )

    assert _registered_names(worker_settings_module.WorkerSettings) == {
        INGEST_DOCUMENT,
        EXTRACT_ENTITIES,
        RESOLVE_ENTITIES,
        DELETE_DOCUMENT,
    }


def test_registered_names_cover_every_enqueueable_task_name(worker_settings_module) -> None:
    """THE regression guard for `function '<name>' not found`.

    `core.events.TASK_NAMES` is every name a caller may pass to `JobQueue.enqueue`. The two
    worker settings classes must register exactly that set between them. A name a caller can
    enqueue but no worker registers is not a loud failure: arq accepts the job, drops it with
    only a worker-side log line, and never runs the task's failure handler — so the document
    sits at PENDING and the enqueuing caller is told nothing.
    """
    registered = _registered_names(worker_settings_module.WorkerSettings) | _registered_names(
        worker_settings_module.ProjectionWorkerSettings
    )
    assert registered == set(TASK_NAMES), (
        f"registered={sorted(registered)} but callers enqueue={sorted(TASK_NAMES)}; "
        "every name in the difference is a job that would be silently dropped"
    )


def test_no_task_name_literals_at_call_sites() -> None:
    """No `enqueue(...)`/`run_task(...)` call may name its task with a string literal.

    This is what makes the drift structurally impossible rather than merely currently-absent:
    the bug was two hand-written copies of one string, and a test that only compares today's
    values would pass again the moment someone adds a sixth task with a fresh literal.
    """
    offenders: list[str] = []
    for path in _PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else None
            if name is None and isinstance(callee, ast.Name):
                name = callee.id
            if name not in _TASK_NAME_CALLEES:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                rel = path.relative_to(_PACKAGE_ROOT.parent)
                offenders.append(f"{rel}:{first.lineno}: {name}({first.value!r}, ...)")
    assert not offenders, (
        "task names must come from graphrag.core.events, not string literals:\n  "
        + "\n  ".join(offenders)
    )


def test_worker_settings_max_tries_from_dead_letter_config(worker_settings_module) -> None:
    assert worker_settings_module.WorkerSettings.max_tries == 3
    assert worker_settings_module.WorkerSettings.retry_jobs is True


def test_worker_settings_job_timeout_from_config(worker_settings_module) -> None:
    """arq's own default is 300s, which silently bounded `extract_entities` — a job that makes
    one sequential `bulk` call per batch of chunks, so its duration scales with the largest
    document rather than with a constant. It must be set, and set from config."""
    from graphrag.config.settings import get_settings

    expected = get_settings().ingestion.job_timeout_s
    assert worker_settings_module.WorkerSettings.job_timeout == expected
    assert worker_settings_module.ProjectionWorkerSettings.job_timeout == expected
    assert create_worker(worker_settings_module.WorkerSettings).job_timeout_s == expected


def test_every_task_module_function_is_registered(worker_settings_module) -> None:
    """BO-08 Step 0 regression guard.

    `project_chunk_payload` (`tasks/project.py`) was, per the task brief, allegedly enqueued by
    `IngestionService` since BO-05 and consumed by nothing — an unregistered arq function is
    dropped with no error the caller sees. Investigation found that claim did NOT hold for this
    repository: `project_chunk_payload` has been correctly named and registered in
    `ProjectionWorkerSettings.functions` (its own dedicated single-concurrency queue/worker,
    `docker-compose.yml`'s `projection-worker` service) since the very first BO-05 commit
    (`c370482`).

    This scan is derived from the `tasks/` directory rather than from a hardcoded list, so a
    fifth task file added later — registered in EITHER settings class — can't repeat this
    silently. It compares against the names arq really registers.
    """
    tasks_dir = Path(importlib.import_module(_TASKS_PACKAGE).__file__).parent
    task_module_names = sorted(
        p.stem for p in tasks_dir.glob("*.py") if p.stem not in _NON_TASK_MODULES
    )
    assert task_module_names, "expected at least one task module under apps/worker/tasks/"

    registered = _registered_names(worker_settings_module.WorkerSettings) | _registered_names(
        worker_settings_module.ProjectionWorkerSettings
    )

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
