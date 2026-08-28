"""`test_endpoints_resolve_per_consumer`. See BUILD_ORDER BO-03 / BLUEPRINT §2.2, §5.1.

The containerized `api` must export telemetry to `otel-collector:4317` while a host-run
consumer (the CLI, this very test process) exports to `localhost:4317` — both from the SAME
`config/{base,local}.yaml`, no file toggling. Two halves:
  1. docker-compose.yml's `api` service sets the container-hostname env overrides.
  2. A host-side `Settings()` (this process; APP_ENV unset -> defaults to "local") resolves the
     localhost values from config/local.yaml.

Marked `integration` (per BUILD_ORDER) even though it needs no running container beyond the
compose file itself — it's asserting cross-BO-03 wiring, not a single unit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from graphrag.config.settings import Settings
from tests.unit._settings_helpers import set_required_secrets

pytestmark = pytest.mark.integration

_COMPOSE_PATH = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def _api_service_environment() -> dict[str, str]:
    compose = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    return compose["services"]["api"]["environment"]


def test_api_container_env_overrides_to_container_hostnames() -> None:
    env = _api_service_environment()
    assert env["GRAPHRAG_OBSERVABILITY__OTLP_ENDPOINT"] == "http://otel-collector:4317"
    assert env["GRAPHRAG_OBSERVABILITY__TRAIL__LOKI_URL"] == "http://otel-lgtm:3100"
    assert env["GRAPHRAG_OBSERVABILITY__TRAIL__TEMPO_URL"] == "http://otel-lgtm:3200"
    assert env["GRAPHRAG_STORES__QDRANT__URL"] == "http://qdrant:6333"
    assert env["GRAPHRAG_STORES__NEO4J__URI"] == "bolt://neo4j:7687"
    assert env["GRAPHRAG_STORES__REDIS__URL"] == "redis://redis:6379/0"


def test_host_settings_resolve_to_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)  # defaults to "local" -> config/local.yaml
    set_required_secrets(monkeypatch)
    settings = Settings()

    assert settings.observability.otlp_endpoint == "http://localhost:4317"
    assert settings.observability.trail.loki_url == "http://localhost:3100"
    assert settings.observability.trail.tempo_url == "http://localhost:3200"
    assert settings.stores.qdrant.url == "http://localhost:6333"
    assert settings.stores.neo4j.uri == "bolt://localhost:7687"
    assert settings.stores.redis.url == "redis://localhost:6379/0"
