"""`GET /v1/debug/trail/{cid}`. See BLUEPRINT §7.1.

Provides debugging information for the trail of a specific correlation ID.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException

from graphrag.apps.api.deps import get_container
from graphrag.apps.api.main import Container

router = APIRouter(prefix="/v1/debug", tags=["debug"])


@router.get("/trail/{cid}")
async def get_trail(
    cid: str,
    admin_key: Annotated[str | None, Header(alias="Admin-Key")] = None,
    container: Container = Depends(get_container),
) -> dict:
    """Admin key required. Returns 404 when `app.env == "prod"` or `trail.enabled` is false."""
    if container.settings.app.env == "prod":
        raise HTTPException(status_code=404, detail="Not Found")

    if container.settings.security.debug_endpoints_require_admin and (
        not admin_key or admin_key != container.settings.secrets.admin_api_key.get_secret_value()
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not container.settings.observability.trail.enabled:
        raise HTTPException(status_code=404, detail="Not Found")

    # Placeholder for actual trail lookup which would require admin key validation
    # and querying the backend for the specific correlation ID.
    return {}
