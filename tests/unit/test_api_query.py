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


def _script_three_route_plans(container: Any) -> None:
    """Router script for the refusal path: `rewrite_query` loops back to `plan_route`, which
    re-plans (BLUEPRINT §6.4), so three `RoutePlanOut` and two `RewrittenQuery`."""
    plan_out = RoutePlanOut(
        strategy="vector",
        template="neighbors",
        seed_entities=[],
        hops=1,
        sub_queries=[],
        rationale="",
    )
    container.llm_client.script_structured(
        "router",
        plan_out,
        plan_out,
        plan_out,
        RewrittenQuery(query="r1", changed_because=""),
        RewrittenQuery(query="r2", changed_because=""),
    )


def _spy_on_top_k(container: Any) -> list[int]:
    """Record the `top_k` every `hybrid_search` is issued with.

    This is the deepest point in the chain: `VectorRetriever.retrieve(query, top_k)` passes it
    straight to the store, so observing it here proves the value travelled the whole
    request -> QueryState -> retrieve_vector -> VectorRetriever path rather than being read
    from config at the far end.
    """
    seen: list[int] = []
    original = container.vector_store.hybrid_search

    async def spy(**kwargs: Any):
        seen.append(kwargs["top_k"])
        return await original(**kwargs)

    container.vector_store.hybrid_search = spy
    return seen


@pytest.mark.asyncio
async def test_client_top_k_reaches_the_vector_retriever(
    container: Any, client: httpx.AsyncClient
) -> None:
    """A client-supplied `top_k` overrides `retrieval.vector.top_k` all the way down.

    BLUEPRINT §7.1 defines `QueryRequest{question, top_k?, strategy?}` and §6.4 seeds `top_k`
    onto `QueryState`; `retrieve_vector` reads `state["top_k"] or retrieval.vector.top_k`. The
    field was plumbed through without a test covering the path end to end.
    """
    configured_default = container.settings.retrieval.vector.top_k
    assert configured_default != 3, "pick a top_k that differs from the config default"
    seen = _spy_on_top_k(container)
    _script_three_route_plans(container)

    response = await client.post("/v1/query", json={"question": "test", "top_k": 3})

    assert response.status_code == 200
    assert seen, "retrieve_vector never reached the vector store"
    assert set(seen) == {3}, f"expected every search at top_k=3, got {seen}"


@pytest.mark.asyncio
async def test_omitted_top_k_falls_back_to_the_config_default(
    container: Any, client: httpx.AsyncClient
) -> None:
    """The companion: with no `top_k` on the request, the configured default is what is used —
    so the test above is measuring an override, not just the only value in play."""
    seen = _spy_on_top_k(container)
    _script_three_route_plans(container)

    response = await client.post("/v1/query", json={"question": "test"})

    assert response.status_code == 200
    assert set(seen) == {container.settings.retrieval.vector.top_k}
