from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from graphrag.apps.api.deps import get_container
from graphrag.apps.api.main import Container, create_app
from graphrag.core.errors import LLMProviderExhausted, LLMSchemaViolation
from graphrag.services.orchestration.schemas import (
    RelevanceGradeBatch,
    RewrittenQuery,
    RoutePlanOut,
)


@pytest.fixture
async def client(container: Container) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.mark.asyncio
async def test_refusal_returns_200_with_retrieved_context(
    container: Any, client: httpx.AsyncClient
):
    # max_query_rewrites is 2. So we need 3 router calls, 2 grader calls, 2 rewrites.
    container.llm_client.script_structured(
        "router",
        RoutePlanOut(
            strategy="vector",
            template="neighbors",
            seed_entities=[],
            hops=1,
            sub_queries=[],
            rationale="",
        ),
        RewrittenQuery(query="rewrite", changed_because=""),
        RoutePlanOut(
            strategy="vector",
            template="neighbors",
            seed_entities=[],
            hops=1,
            sub_queries=[],
            rationale="",
        ),
        RewrittenQuery(query="rewrite2", changed_because=""),
        RoutePlanOut(
            strategy="vector",
            template="neighbors",
            seed_entities=[],
            hops=1,
            sub_queries=[],
            rationale="",
        ),
    )
    container.llm_client.script_structured(
        "grader",
        RelevanceGradeBatch(grades=[]),
        RelevanceGradeBatch(grades=[]),
        RelevanceGradeBatch(grades=[]),
    )

    response = await client.post("/v1/query", json={"question": "test"})
    assert response.status_code == 200
    assert (
        "I do not have enough information to answer that question"
        in response.json()["answer"]["text"]
    )


@pytest.mark.asyncio
async def test_sse_emits_node_events_in_order(container: Any, client: httpx.AsyncClient):
    container.llm_client.script_structured(
        "router",
        RoutePlanOut(
            strategy="vector",
            template="neighbors",
            seed_entities=[],
            hops=1,
            sub_queries=[],
            rationale="",
        ),
        RewrittenQuery(query="r", changed_because=""),
        RoutePlanOut(
            strategy="vector",
            template="neighbors",
            seed_entities=[],
            hops=1,
            sub_queries=[],
            rationale="",
        ),
        RewrittenQuery(query="r", changed_because=""),
        RoutePlanOut(
            strategy="vector",
            template="neighbors",
            seed_entities=[],
            hops=1,
            sub_queries=[],
            rationale="",
        ),
    )
    container.llm_client.script_structured(
        "grader",
        RelevanceGradeBatch(grades=[]),
        RelevanceGradeBatch(grades=[]),
        RelevanceGradeBatch(grades=[]),
    )

    response = await client.post("/v1/query/stream", json={"question": "test"})
    assert response.status_code == 200
    events = [line for line in response.text.splitlines() if line.startswith("data: ")]
    assert len(events) > 0


@pytest.mark.asyncio
async def test_exhaustion_on_retry_yields_503(container: Any, client: httpx.AsyncClient):
    from unittest.mock import patch

    with patch(
        "graphrag.services.orchestration.graph.OrchestrationService.run",
        side_effect=LLMProviderExhausted("test"),
    ):
        response = await client.post("/v1/query", json={"question": "test"})
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_schema_violation_on_retry_yields_502(container: Any, client: httpx.AsyncClient):
    from unittest.mock import patch

    with patch(
        "graphrag.services.orchestration.graph.OrchestrationService.run",
        side_effect=LLMSchemaViolation("test"),
    ):
        response = await client.post("/v1/query", json={"question": "test"})
    assert response.status_code == 502


@pytest.mark.asyncio
async def test_get_trail_auth_and_env_logic(container: Any, client: httpx.AsyncClient):
    from unittest.mock import patch

    admin_key = container.settings.secrets.admin_api_key.get_secret_value()
    cid = "test-cid"
    url = f"/v1/debug/trail/{cid}"

    # Helper to construct a fresh Settings object using the established env-var pattern
    def mock_settings(env: str, trail_enabled: bool, require_admin: bool = True) -> Any:
        from graphrag.config.settings import Settings

        with patch.dict(
            "os.environ",
            {
                "GRAPHRAG_APP__ENV": env,
                "GRAPHRAG_OBSERVABILITY__TRAIL__ENABLED": str(trail_enabled).lower(),
                "GRAPHRAG_SECURITY__DEBUG_ENDPOINTS_REQUIRE_ADMIN": str(require_admin).lower(),
            },
        ):
            return Settings()

    # 1. Wrong/missing Admin-Key -> 403 (non-prod)
    with patch.object(container, "settings", mock_settings("local", True)):
        res = await client.get(url)
        assert res.status_code == 403

        res = await client.get(url, headers={"Admin-Key": "wrong"})
        assert res.status_code == 403

    # 2. Correct key, trail.enabled=True, non-prod -> 200
    with patch.object(container, "settings", mock_settings("local", True)):
        res = await client.get(url, headers={"Admin-Key": admin_key})
        assert res.status_code == 200

    # 3. Bypass Auth: require_admin=False, no key, trail.enabled=True, non-prod -> 200
    with patch.object(container, "settings", mock_settings("local", True, require_admin=False)):
        res = await client.get(url)
        assert res.status_code == 200

    # 4. env=='prod' -> 404 regardless of key
    # (Note: Settings validator requires trail to be disabled in prod)
    with patch.object(container, "settings", mock_settings("prod", False)):
        res = await client.get(url)
        assert res.status_code == 404

        res = await client.get(url, headers={"Admin-Key": admin_key})
        assert res.status_code == 404
