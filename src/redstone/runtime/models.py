"""Runtime state machine.

Mirrors the pattern established by redstone.agent.models.can_transition: a
single, un-bypassable choke point for whether one state may move to another.
`Runtime` itself lives in redstone.domain.models (Phase 1 already declared it;
Phase 5 extends it additively and adds the transition policy here, in the
subsystem that owns the business logic -- domain.models stays data-shape-only).
"""

from __future__ import annotations

from ..domain.models import Runtime, RuntimeState
from .errors import RedstoneRuntimeError, RuntimeErrorCode

__all__ = ["TERMINAL_RUNTIME_STATES", "can_transition_runtime", "transition"]

# "No longer the workspace's active runtime" -- RuntimeManager releases the
# one-active-runtime slot when a runtime reaches any of these. Distinct from
# _FULLY_TERMINAL below: STOPPED/FAILED/KILLED are "resting" states that can
# still move on to DESTROYED (an explicit cleanup action), whereas
# DESTROYED/EXPIRED accept no transition at all.
TERMINAL_RUNTIME_STATES = frozenset(
    {RuntimeState.STOPPED, RuntimeState.FAILED, RuntimeState.KILLED,
     RuntimeState.DESTROYED, RuntimeState.EXPIRED}
)

_FULLY_TERMINAL = frozenset({RuntimeState.DESTROYED, RuntimeState.EXPIRED})
_RESTING = frozenset({RuntimeState.STOPPED, RuntimeState.FAILED, RuntimeState.KILLED})

_ACTIVE_TRANSITIONS: dict[RuntimeState, frozenset[RuntimeState]] = {
    # STOPPING is reachable directly from CREATED too: stop() on a runtime
    # that was created but never started has nothing to gracefully stop, but
    # must still behave safely rather than raising INVALID_TRANSITION.
    RuntimeState.CREATED: frozenset({RuntimeState.STARTING, RuntimeState.STOPPING}),
    RuntimeState.STARTING: frozenset({RuntimeState.RUNNING}),
    RuntimeState.RUNNING: frozenset({RuntimeState.IDLE, RuntimeState.STOPPING}),
    RuntimeState.IDLE: frozenset({RuntimeState.RUNNING, RuntimeState.STOPPING}),
    RuntimeState.STOPPING: frozenset({RuntimeState.STOPPED}),
}


def can_transition_runtime(current: RuntimeState, target: RuntimeState) -> bool:
    """Whether `current` may move to `target`.

    DESTROYED/EXPIRED never move again. A "resting" state (STOPPED/FAILED/
    KILLED) may only move on to DESTROYED -- an explicit cleanup action, not a
    productive one. Any other non-terminal state may move directly to FAILED,
    KILLED or DESTROYED (a crash, an operator kill, or an explicit destroy can
    happen from anywhere); the remaining "productive" transitions follow the
    explicit graph above.
    """
    if current in _FULLY_TERMINAL:
        return False
    if current in _RESTING:
        return target is RuntimeState.DESTROYED
    if target in (RuntimeState.FAILED, RuntimeState.KILLED, RuntimeState.DESTROYED):
        return True
    return target in _ACTIVE_TRANSITIONS.get(current, frozenset())


def transition(runtime: Runtime, target: RuntimeState) -> Runtime:
    """Validated state change. `Runtime.with_state()` itself is a plain
    setter (domain.models stays policy-free); this is the single choke point
    every RuntimeManager operation goes through instead of calling
    with_state() directly."""
    if not can_transition_runtime(runtime.state, target):
        raise RedstoneRuntimeError(
            RuntimeErrorCode.INVALID_TRANSITION,
            safe_message=f"Cannot move runtime from '{runtime.state.value}' to '{target.value}'.",
        )
    return runtime.with_state(target)
