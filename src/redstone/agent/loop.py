"""The bounded agent loop.

One iteration is one AI round-trip. The model's reply must be exactly one JSON
action object (enforced by the system prompt in ``prompt.py``); this module
parses it defensively — a model reply is exactly as untrusted as a project
file — and never executes anything the parsed object does not explicitly and
validly request.

The loop calls only ``AIGateway.generate`` and ``ToolRegistry.call``. It has no
import of a provider class and no code path that runs a shell command, an
`eval`, or a project script.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from ..ai import AIGateway, AIRequest, RedstoneAIError
from ..changes.snapshots import SnapshotStore, capture_state, diff_states
from ..config import Limits
from ..domain.models import ChangeKind, EventType, new_id
from .context import build_ai_messages, render_tool_result
from .errors import AgentErrorCode
from .models import AgentMessage, AgentStatus, AgentStep, AgentTask, StepType
from .tools.registry import ToolContext, ToolRegistry

__all__ = ["run_agent_task"]

# Maps a tool's `kind` to the state the task moves into while running it.
_KIND_TO_STATUS = {
    "read": AgentStatus.INSPECTING,
    "mutate": AgentStatus.EDITING,
    "validate": AgentStatus.VALIDATING,
}

_MAX_MALFORMED_NOTICES_LOGGED = 200  # defensive cap; not a security boundary


@dataclass(frozen=True, slots=True)
class _Action:
    kind: str                 # "tool_call" | "message" | "complete"
    tool: str | None = None
    arguments: dict | None = None
    text: str | None = None
    summary: str | None = None


def _parse_action(raw_text: str) -> _Action | None:
    """Parse the model's reply as exactly one JSON action object.

    Returns None for anything that is not a well-formed, recognised action —
    malformed JSON, a JSON value that is not an object, an unknown `action`
    field, or a tool_call missing `tool`/`arguments`. The caller treats None as
    a recoverable, bounded failure, not a crash.
    """
    text = (raw_text or "").strip()
    # Tolerate a model that wraps the object in a markdown fence despite being
    # told not to; still a plain, non-executing string operation.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None

    if not isinstance(parsed, dict):
        return None

    action = parsed.get("action")
    if action == "tool_call":
        tool = parsed.get("tool")
        arguments = parsed.get("arguments", {})
        if not isinstance(tool, str) or not isinstance(arguments, dict):
            return None
        return _Action(kind="tool_call", tool=tool, arguments=arguments)
    if action == "message":
        text_value = parsed.get("text", "")
        if not isinstance(text_value, str):
            return None
        return _Action(kind="message", text=text_value)
    if action == "complete":
        summary = parsed.get("summary", "")
        if not isinstance(summary, str):
            return None
        return _Action(kind="complete", summary=summary)

    return None


def run_agent_task(
    task: AgentTask,
    *,
    gateway: AIGateway,
    registry: ToolRegistry,
    context: ToolContext,
    snapshot_store: SnapshotStore,
    limits: Limits,
    on_update: Callable[[AgentTask], None] = lambda t: None,
    on_event: Callable[[EventType, dict], None] = lambda e, p: None,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> AgentTask:
    """Run `task` to completion, failure, cancellation or its iteration/time
    limit. Returns the final AgentTask; never raises for a normal AI or tool
    failure — those become a FAILED task with `task.error` set. An unexpected
    internal exception is still converted to a FAILED task rather than
    propagating, so a caller (the service, an API route) never has to guess
    whether a task "half-happened"."""

    started_at = time.monotonic()
    tool_specs = registry.catalog()

    task = task.with_status(AgentStatus.PLANNING)
    on_update(task)
    on_event(EventType.AGENT_STARTED, {"task_id": task.id})

    before_state = capture_state(context.workspace.project_root, limits)

    try:
        while task.iterations_used < limits.max_agent_iterations:
            if is_cancelled():
                task = task.with_status(AgentStatus.CANCELLED)
                on_update(task)
                on_event(EventType.AGENT_CANCELLED, {"task_id": task.id})
                return task

            if time.monotonic() - started_at > limits.max_agent_timeout:
                task = task.with_status(AgentStatus.TIMED_OUT).with_error(
                    AgentErrorCode.TIMEOUT.value, "The agent task exceeded its time limit."
                )
                on_update(task)
                on_event(EventType.AGENT_FAILED, {"task_id": task.id, "reason": "timeout"})
                return task

            task = task.with_iteration()
            on_event(
                EventType.AGENT_STEP_STARTED,
                {"task_id": task.id, "iteration": task.iterations_used},
            )

            ai_messages = build_ai_messages(
                task.messages, tool_specs, max_context_bytes=limits.max_agent_context_bytes
            )
            request = AIRequest(messages=ai_messages, request_id=task.id)

            try:
                response = gateway.generate(request)
            except RedstoneAIError as exc:
                task = task.with_step(
                    AgentStep.create(StepType.ERROR, {"error_code": exc.code.value})
                ).with_status(AgentStatus.FAILED).with_error(exc.code.value, exc.safe_message)
                on_update(task)
                on_event(EventType.AGENT_FAILED, {"task_id": task.id, "error_code": exc.code.value})
                return task

            task = task.with_message(AgentMessage(role="assistant", content=response.text))

            action = _parse_action(response.text)
            if action is None:
                task = task.with_step(
                    AgentStep.create(StepType.ERROR, {"reason": "malformed_action"})
                )
                task = task.with_message(
                    AgentMessage(
                        role="user",
                        content=(
                            "Your last response was not a single valid JSON action "
                            "object. Respond with exactly one JSON object as instructed."
                        ),
                    )
                )
                on_update(task)
                continue

            if action.kind == "message":
                task = task.with_step(AgentStep.create(StepType.ASSISTANT, {"text": action.text}))
                on_update(task)
                continue

            if action.kind == "tool_call":
                task, terminal = _handle_tool_call(
                    task, action, registry, context, snapshot_store, on_event
                )
                on_update(task)
                if terminal:
                    return task
                continue

            if action.kind == "complete":
                task = _handle_completion(task, action, context, before_state, on_event)
                on_update(task)
                return task

        # Exhausted the iteration budget without reaching a terminal action.
        task = task.with_status(AgentStatus.TIMED_OUT).with_error(
            AgentErrorCode.ITERATION_LIMIT.value,
            f"Reached the {limits.max_agent_iterations}-iteration limit before finishing.",
        )
        on_update(task)
        on_event(EventType.AGENT_FAILED, {"task_id": task.id, "reason": "iteration_limit"})
        return task

    except Exception as exc:  # noqa: BLE001 - a task must always end in a terminal state
        task = task.with_step(
            AgentStep.create(StepType.ERROR, {"reason": type(exc).__name__})
        ).with_status(AgentStatus.FAILED).with_error(
            AgentErrorCode.INTERNAL_ERROR.value, "The agent task failed unexpectedly."
        )
        on_update(task)
        on_event(EventType.AGENT_FAILED, {"task_id": task.id, "error_code": "internal"})
        return task


def _handle_tool_call(
    task: AgentTask,
    action: _Action,
    registry: ToolRegistry,
    context: ToolContext,
    snapshot_store: SnapshotStore,
    on_event: Callable[[EventType, dict], None],
) -> tuple[AgentTask, bool]:
    """Execute one tool call. Returns (updated_task, is_terminal)."""
    call_id = new_id("call")
    task = task.with_step(
        AgentStep.create(StepType.TOOL_CALL, {"call_id": call_id, "tool": action.tool})
    )

    kind = registry.kind_of(action.tool)
    is_validation = kind == "validate"
    if is_validation:
        on_event(EventType.AGENT_VALIDATION_STARTED, {"task_id": task.id, "tool": action.tool})
    on_event(EventType.AGENT_TOOL_STARTED, {"task_id": task.id, "tool": action.tool})

    result = registry.call(call_id, action.tool, action.arguments, context)

    on_event(
        EventType.AGENT_TOOL_COMPLETED,
        {"task_id": task.id, "tool": action.tool, "ok": result.ok},
    )
    if is_validation:
        on_event(
            EventType.AGENT_VALIDATION_COMPLETED,
            {"task_id": task.id, "tool": action.tool, "ok": result.ok},
        )

    task = task.with_step(
        AgentStep.create(
            StepType.TOOL_RESULT,
            {"call_id": call_id, "tool": action.tool, "ok": result.ok,
             "error_code": result.error_code},
        )
    )

    target_status = _KIND_TO_STATUS.get(kind, AgentStatus.INSPECTING)
    if task.status != target_status:
        task = task.with_status(target_status)

    if kind == "mutate" and result.ok and task.snapshot_id is None:
        snapshot = snapshot_store.create(task.project_id, label=f"before task {task.id}")
        task = task.with_snapshot(snapshot.id)
        on_event(EventType.SNAPSHOT_CREATED, {"task_id": task.id, "snapshot_id": snapshot.id})

    result_text = render_tool_result(
        action.tool,
        {"ok": result.ok, "output": result.output, "error_code": result.error_code,
         "truncated": result.truncated},
    )
    task = task.with_message(AgentMessage(role="user", content=result_text))

    return task, False


def _handle_completion(
    task: AgentTask,
    action: _Action,
    context: ToolContext,
    before_state,
    on_event: Callable[[EventType, dict], None],
) -> AgentTask:
    after_state = capture_state(context.workspace.project_root)
    changeset = diff_states(
        before_state, after_state, context.workspace.project_root,
        project_id=task.project_id, task_id=task.id, reason=action.summary or "",
    )

    task = task.with_step(
        AgentStep.create(
            StepType.COMPLETION,
            {
                "summary": action.summary,
                "changeset": {
                    "id": changeset.id,
                    "created": [c.path for c in changeset.of_kind(ChangeKind.CREATED)],
                    "modified": [c.path for c in changeset.of_kind(ChangeKind.MODIFIED)],
                    "deleted": [c.path for c in changeset.of_kind(ChangeKind.DELETED)],
                },
            },
        )
    )
    task = task.with_changeset(changeset.id)
    task = task.with_status(AgentStatus.COMPLETED)
    on_event(EventType.AGENT_COMPLETED, {"task_id": task.id, "changeset_id": changeset.id})
    return task
