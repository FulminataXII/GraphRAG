"""`JobStatus.state` terminal/non-terminal partition. See BLUEPRINT §3.2 / BO-05.

`JobStatus` is deliberately distinct from `DocumentStatus` (a job can be `complete` while its
document is `FAILED`) — this guards its OWN small state machine: once a job reaches a terminal
state, arq will never move it again. A future edit that adds a new `Literal` value to
`JobStatus.state` without updating the partition below is exactly the regression this catches.
"""

from __future__ import annotations

from typing import get_args

from graphrag.core.models import JobStatus

TERMINAL_STATES = frozenset({"complete", "failed", "not_found"})
NON_TERMINAL_STATES = frozenset({"deferred", "queued", "in_progress"})


def _declared_states() -> frozenset[str]:
    (state_field,) = (f for name, f in JobStatus.model_fields.items() if name == "state")
    return frozenset(get_args(state_field.annotation))


def test_job_state_machine_terminal_states() -> None:
    declared = _declared_states()
    assert declared == TERMINAL_STATES | NON_TERMINAL_STATES, (
        "TERMINAL_STATES/NON_TERMINAL_STATES must be updated to match JobStatus.state's "
        f"Literal values; declared={sorted(declared)}"
    )
    assert frozenset() == TERMINAL_STATES & NON_TERMINAL_STATES
