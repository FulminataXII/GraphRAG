from __future__ import annotations

from typing import TYPE_CHECKING, Any

from graphrag.core.errors import LLMSchemaViolation
from graphrag.core.models import NodeFailure, RoutePlan, Spend

if TYPE_CHECKING:
    from graphrag.services.orchestration.graph import NodeDeps
from graphrag.services.orchestration.prompts import render
from graphrag.services.orchestration.schemas import RoutePlanOut
from graphrag.services.orchestration.state import QueryState


async def node(state: QueryState, deps: NodeDeps) -> dict[str, Any]:
    # No "plan already set -> return {}" short-circuit. That branch existed only to let a
    # client-supplied `strategy` bypass the router by pre-seeding a whole fabricated RoutePlan,
    # and the plan it fabricated carried `seed_entities=[]`, which made every graph traversal
    # return nothing. The client's choice now arrives as `strategy_override` and is applied
    # below, so the router always runs and always supplies the semantics. BLUEPRINT §6.4's edge
    # list routes `rewrite_query -> plan_route`, which re-plans, as it did before the override
    # was bolted on.
    question = state["question"]
    # Policy from the client when supplied, semantics from the router either way.
    override = state.get("strategy_override")
    prompt = render("route_plan.j2", question=question)

    try:
        result = await deps.llm.structured(
            role="router",
            messages=[{"role": "user", "content": prompt}],
            schema=RoutePlanOut,
            max_repairs=deps.settings.orchestration.max_structured_output_repairs,
        )
        plan_out = result.value
        plan = RoutePlan(
            strategy=override or plan_out.strategy,
            template=plan_out.template,
            seed_entities=plan_out.seed_entities,
            hops=plan_out.hops,
            relation_type=plan_out.relation_type,
            sub_queries=plan_out.sub_queries,
            rationale=plan_out.rationale,
        )
        spent = Spend(
            llm_calls=result.repair_attempts + 1,
            tokens=result.prompt_tokens + result.completion_tokens,
            wall_ms=result.latency_ms,
        )
        deps.metrics.route_selected.add(1, {"strategy": plan.strategy})
        return {
            "plan": plan,
            "spent": spent,
            "attempts": {"plan_route": 1},
        }
    except LLMSchemaViolation as e:
        # Fail open (BLUEPRINT §6.4). An explicit client strategy still wins over the
        # configured default — it is the one part of the plan that did not depend on the
        # router succeeding.
        plan = RoutePlan(
            strategy=override or deps.settings.orchestration.default_strategy,
            template="neighbors",
            seed_entities=[],
            hops=1,
            relation_type=None,
            sub_queries=[],
            rationale="Fail open",
        )
        deps.metrics.route_selected.add(1, {"strategy": plan.strategy})
        return {
            "plan": plan,
            "failures": [
                NodeFailure(
                    node="plan_route",
                    code=e.code,
                    message=str(e),
                    attempt=state.get("attempts", {}).get("plan_route", 0) + 1,
                    at=deps.clock.now(),
                )
            ],
            "attempts": {"plan_route": 1},
            # spent is not recorded on failure in this MVP, though it could be.
            # BLUEPRINT test test_router_fails_open_on_schema_violation requires fails open.
        }
