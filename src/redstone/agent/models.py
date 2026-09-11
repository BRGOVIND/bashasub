"""Agent domain model.

Framework-independent, like the rest of ``redstone.domain``. Everything here is
a frozen dataclass; an ``AgentTask`` advances by producing a new instance
through ``with_status``/``with_step``, never by mutating fields in place, which
is what makes the state machine's transition check a single, un-bypassable
choke point.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum

from ..agent.errors import AgentErrorCode, RedstoneAgentError
from ..domain.models import new_id, utcnow

__all__ = [
    "AgentStatus",
    "StepType",
    "AgentMessage",
    "ToolCall",
    "ToolResult",
    "AgentStep",
    "AgentTask",
    "TERMINAL_STATUSES",
    "can_transition",
]


class AgentStatus(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    INSPECTING = "inspecting"
    EDITING = "editing"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class StepType(str, Enum):
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    VALIDATION = "validation"
    ERROR = "error"
    COMPLETION = "completion"


TERMINAL_STATUSES = frozenset(
    {AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.CANCELLED, AgentStatus.TIMED_OUT}
)

# The active-state graph. Any active state may also move directly to FAILED,
# CANCELLED or TIMED_OUT (checked separately below), so those are not repeated
# in every row.
_ACTIVE_TRANSITIONS: dict[AgentStatus, frozenset[AgentStatus]] = {
    AgentStatus.CREATED: frozenset({AgentStatus.PLANNING}),
    AgentStatus.PLANNING: frozenset(
        {AgentStatus.INSPECTING, AgentStatus.EDITING, AgentStatus.VALIDATING,
         AgentStatus.COMPLETED}
    ),
    AgentStatus.INSPECTING: frozenset(
        {AgentStatus.PLANNING, AgentStatus.EDITING, AgentStatus.VALIDATING,
         AgentStatus.COMPLETED}
    ),
    AgentStatus.EDITING: frozenset(
        {AgentStatus.PLANNING, AgentStatus.INSPECTING, AgentStatus.VALIDATING,
         AgentStatus.COMPLETED}
    ),
    AgentStatus.VALIDATING: frozenset(
        {AgentStatus.PLANNING, AgentStatus.INSPECTING, AgentStatus.EDITING,
         AgentStatus.COMPLETED}
    ),
}


def can_transition(current: AgentStatus, target: AgentStatus) -> bool:
    """Whether `current` may move to `target`.

    Terminal states never move again. Any non-terminal state may move to
    FAILED, CANCELLED or TIMED_OUT from wherever it is; the remaining
    "productive" transitions follow the explicit graph above.
    """
    if current in TERMINAL_STATUSES:
        return False
    if target in (AgentStatus.FAILED, AgentStatus.CANCELLED, AgentStatus.TIMED_OUT):
        return True
    return target in _ACTIVE_TRANSITIONS.get(current, frozenset())


@dataclass(frozen=True, slots=True)
class AgentMessage:
    """One turn of the agent's conversation record.

    Deliberately a separate type from ``redstone.ai.AIMessage``: this is what
    the agent *stores*, translated to the AI layer's type only at the point of
    calling the gateway. `role` is "system" | "user" | "assistant". Content
    coming from tool results is still role "user" — it is data the assistant
    reads, never a system-level instruction — see agent/context.py.
    """

    role: str
    content: str
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    tool: str
    arguments: dict


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    tool: str
    ok: bool
    output: dict = field(default_factory=dict)
    error_code: str | None = None
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class AgentStep:
    id: str
    type: StepType
    content: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)

    @classmethod
    def create(cls, step_type: StepType, content: dict) -> AgentStep:
        return cls(id=new_id("step"), type=step_type, content=content)


@dataclass(frozen=True, slots=True)
class AgentTask:
    """One run of the agent loop against one workspace.

    `project_id` and `workspace_id` are set once at creation and there is no
    method that changes them: the workspace association cannot be altered by
    anything that happens during the task, including the model.
    """

    id: str
    project_id: str
    workspace_id: str
    status: AgentStatus = AgentStatus.CREATED
    messages: tuple[AgentMessage, ...] = ()
    steps: tuple[AgentStep, ...] = ()
    iterations_used: int = 0
    snapshot_id: str | None = None
    changeset_id: str | None = None
    error: dict | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @classmethod
    def create(cls, project_id: str, workspace_id: str, request_message: str) -> AgentTask:
        return cls(
            id=new_id("task"),
            project_id=project_id,
            workspace_id=workspace_id,
            messages=(AgentMessage(role="user", content=request_message),),
        )

    def with_status(self, status: AgentStatus) -> AgentTask:
        if not can_transition(self.status, status):
            raise RedstoneAgentError(
                AgentErrorCode.INVALID_TRANSITION,
                safe_message=f"Cannot move task from '{self.status.value}' to '{status.value}'.",
            )
        return replace(self, status=status, updated_at=utcnow())

    def with_message(self, message: AgentMessage) -> AgentTask:
        return replace(self, messages=self.messages + (message,), updated_at=utcnow())

    def with_step(self, step: AgentStep) -> AgentTask:
        return replace(self, steps=self.steps + (step,), updated_at=utcnow())

    def with_iteration(self) -> AgentTask:
        return replace(self, iterations_used=self.iterations_used + 1, updated_at=utcnow())

    def with_snapshot(self, snapshot_id: str) -> AgentTask:
        return replace(self, snapshot_id=snapshot_id, updated_at=utcnow())

    def with_changeset(self, changeset_id: str) -> AgentTask:
        return replace(self, changeset_id=changeset_id, updated_at=utcnow())

    def with_error(self, error_code: str, message: str) -> AgentTask:
        return replace(
            self, error={"error_code": error_code, "message": message}, updated_at=utcnow()
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict:
        """The safe, API-facing view. Never includes message/step content
        that could carry file contents or provider detail — callers needing
        that use the events history instead."""
        return {
            "task_id": self.id,
            "project_id": self.project_id,
            "status": self.status.value,
            "current_step": self.steps[-1].type.value if self.steps else None,
            "iterations_used": self.iterations_used,
            "changeset_id": self.changeset_id,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
