"""FastAPI dependencies. See BLUEPRINT §7.1.

Only `get_container` lands in this build order. `require_api_key` / `require_admin` need
`ApiKey` (BLUEPRINT §1a Type Index: `apps/api/deps.py`, BO-12) — adding them now would be
building ahead of the current BO; BUILD_ORDER.md's BO-03 list names `get_container` only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from graphrag.apps.api.main import Container


def get_container(request: Request) -> Container:
    """Reads request.app.state.container.

    A FastAPI dependency has NO implicit access to lifespan state — it must take Request.
    """
    return request.app.state.container  # type: ignore[no-any-return]
