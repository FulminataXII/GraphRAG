"""`/healthz` and `/readyz`. See BLUEPRINT §7.1.

Never conflate them: `healthz` is "is the process alive" (no I/O — a dependency blip must never
restart a healthy process), `readyz` is "can this instance actually serve" (backend probes,
cached, deadline-bounded).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from graphrag.apps.api.deps import get_container
from graphrag.apps.api.main import Container

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: process alive. Always 200 while serving. Performs NO I/O."""
    return {"status": "ok"}


class ReadyzError(BaseModel):
    status: Literal["not_ready"]
    failing: list[str]


@router.get(
    "/readyz", responses={503: {"model": ReadyzError, "description": "Service Unavailable"}}
)
async def readyz(container: Container = Depends(get_container)) -> JSONResponse:
    """Readiness: concurrent, deadline-bounded backend probes, cached for
    `app.readyz_cache_s` seconds. 503 naming every failing backend when any probe fails."""
    results = await container.readyz()
    failing = sorted(name for name, healthy in results.items() if not healthy)
    if failing:
        return JSONResponse(status_code=503, content={"status": "not_ready", "failing": failing})
    return JSONResponse(status_code=200, content={"status": "ok"})
