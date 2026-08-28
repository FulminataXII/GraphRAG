"""`/healthz` and `/readyz` against the real, containerized `api` service. See BLUEPRINT §7.1.

Requires `make up` — the `api` service must already be running and healthy. Drives the same
`docker compose stop/start qdrant` sequence a human runs by hand per BUILD_ORDER's regression
table.
"""

from __future__ import annotations

import subprocess
import time

import httpx
import pytest

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost:8000"


def _compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], check=True, capture_output=True)


@pytest.fixture
def restart_qdrant_after():
    yield
    _compose("start", "qdrant")
    # readyz caches for app.readyz_cache_s (5s in base.yaml) — wait it out so the container's
    # own next probe round sees qdrant healthy again, for whichever test runs next.
    time.sleep(6)


def test_healthz_up_readyz_down(restart_qdrant_after: None) -> None:
    with httpx.Client(base_url=_BASE_URL, timeout=10.0) as client:
        healthy = client.get("/readyz")
        assert healthy.status_code == 200

        _compose("stop", "qdrant")
        time.sleep(6)  # let the previous readyz cache window expire

        readyz = client.get("/readyz")
        assert readyz.status_code == 503
        assert "qdrant" in readyz.json()["failing"]

        healthz = client.get("/healthz")
        assert healthz.status_code == 200  # liveness must never follow readiness down
