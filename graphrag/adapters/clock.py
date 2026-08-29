"""`SystemClock` — implements `core.ports.Clock` with the real wall clock.

BLUEPRINT §1a's Type Index names no concrete `Clock` implementation anywhere (only the
Protocol in `core/ports.py` and `tests.fakes.FakeClock`) — a small spec gap, reported alongside
the rest of BO-05. `core/` and `services/` may never call `datetime.now()` directly (BLUEPRINT
§0), so something in `adapters/` or `apps/` has to supply the real thing the injected `Clock`
port describes; this is that implementation, used to construct `IngestionService` et al. in the
worker (`apps/worker/tasks/`).
"""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
