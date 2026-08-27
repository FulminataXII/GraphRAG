"""[G]-adjacent integration test: `make up` (optionally `obs=1`) must bring every declared
container to a healthy state within 120s.

NOTE: BUILD_ORDER.md says "all 7 services healthy". The compose files as specified in
BLUEPRINT §1 / ARCHITECTURE §7.2 declare 5 core services (qdrant, neo4j, postgres, redis,
litellm) + 3 obs services (otel-collector, otel-lgtm, phoenix) = 8, not 7. This test checks
whatever is actually declared/running rather than hardcoding a count, so it isn't coupled to
that discrepancy — flagged for a human to reconcile the docs.

Requires `docker compose --profile core [--profile obs] up -d` to already be running
(`make up` / `make up obs=1`). Excluded from `make test` by the `integration` marker.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest

pytestmark = pytest.mark.integration


def _compose_ps() -> list[dict]:
    # --all is load-bearing: plain `ps` only lists RUNNING containers, so a service that
    # crashed and exited (e.g. litellm with no/bad config) simply doesn't appear — the loop
    # below would then see only the survivors, find them all "healthy", and false-pass while
    # a dead container sits right next to them. Silent success is exactly the failure mode
    # this test exists to catch.
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "core",
            "--profile",
            "obs",
            "ps",
            "--all",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _summarize(services: list[dict]) -> str:
    """One line per service: name, state, health. Not the full `docker compose ps` JSON —
    that's ~200 lines of Docker labels to find one word ("unhealthy") in."""
    if not services:
        return "(no services reported)"
    return "\n".join(
        f"{s.get('Service', '?'):12s} {s.get('State', '?'):10s} {s.get('Health') or '-'}"
        for s in services
    )


def test_stack_healthy() -> None:
    deadline = time.monotonic() + 120
    services: list[dict] = []
    while time.monotonic() < deadline:
        services = _compose_ps()
        if services and all(s.get("Health") == "healthy" for s in services):
            break
        time.sleep(2)
    else:
        pytest.fail(f"stack did not become healthy within 120s:\n{_summarize(services)}")

    assert services, "no services reported by `docker compose ps` — is the stack up?"
    for service in services:
        assert service.get("Health") == "healthy", _summarize([service])
