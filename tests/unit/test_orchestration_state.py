from __future__ import annotations

import operator
from typing import get_args, get_type_hints

from graphrag.core.models import Spend
from graphrag.services.orchestration.state import QueryState, merge_counters


def test_state_schema_has_reducers_on_multiwriter_keys():
    hints = get_type_hints(QueryState, include_extras=True)

    assert get_args(hints["degraded"])[1] == operator.add
    assert get_args(hints["failures"])[1] == operator.add
    assert get_args(hints["attempts"])[1] == merge_counters
    assert get_args(hints["spent"])[1] == Spend.merge


def test_attempts_counter_merges():
    a = {"grade_context": 1, "plan_route": 1}
    b = {"grade_context": 1, "rewrite_query": 1}
    assert merge_counters(a, b) == {"grade_context": 2, "plan_route": 1, "rewrite_query": 1}
