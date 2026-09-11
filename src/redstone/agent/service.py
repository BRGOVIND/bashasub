"""AgentService: the top-level entry point for starting and observing tasks.

Owns an in-memory project→workspace registry (no database, per Phase 3 scope)
and enforces "one active agent task per workspace" with a *non-blocking*
per-workspace admission lock. This is a different concern from
``workspace.safety.workspace_lock``, which every file/snapshot operation
already acquires internally to make a single filesystem operation atomic: that
lock is short-held and blocking (an FS op waits its turn), because the thing
it protects finishes in milliseconds. Admission control protects a much longer
unit of work — an entire agent task, which can span many AI round-trips — and
must fail fast rather than queue a second caller behind an open-ended wait, so
it needs a lock of its own, checked with a non-blocking acquire.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from ..ai import AIGateway
from ..changes.snapshots import SnapshotStore
from ..config import RedstoneConfig
from ..domain.models import EventType, Framework, Project, ProjectStatus
from ..events.bus import Event, EventBus
from ..workspace.manager import WorkspaceManager
from .errors import AgentErrorCode, RedstoneAgentError
from .loop import run_agent_task
from .models import AgentStatus, AgentTask
from .tools.registry import ToolContext, ToolRegistry, default_registry
from .tools.validation import UnavailableValidationRunner, ValidationRunner

__all__ = ["AgentService"]


@dataclass
class _WorkspaceLease:
    """Per-workspace admission lock plus the active task id, if any."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    active_task_id: str | None = None


class AgentService:
    def __init__(
        self,
        config: RedstoneConfig,
        *,
        gateway: AIGateway | None = None,
        registry: ToolRegistry | None = None,
        validation_runner: ValidationRunner | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._config = config
        self._workspaces = WorkspaceManager(config.workspaces_root, config.limits)
        self._gateway = gateway or AIGateway(config.ai)
        self._registry = registry or default_registry()
        self._validation_runner = validation_runner or UnavailableValidationRunner()
        self._events = event_bus or EventBus()

        self._projects_lock = threading.Lock()
        self._projects: dict[str, Project] = {}

        self._leases_lock = threading.Lock()
        self._leases: dict[str, _WorkspaceLease] = {}

        self._tasks_lock = threading.Lock()
        self._tasks: dict[str, AgentTask] = {}
        self._task_events: dict[str, list[Event]] = {}
        self._cancel_flags: dict[str, threading.Event] = {}

    # --------------------------------------------------------------- projects

    def create_project(self, name: str, framework: Framework = Framework.STATIC) -> Project:
        if not isinstance(name, str) or not name.strip():
            raise RedstoneAgentError(
                AgentErrorCode.INVALID_REQUEST, safe_message="Project name must not be empty."
            )

        project = Project.create(name.strip(), framework)
        self._workspaces.create(project.workspace_id)
        project = project.with_status(ProjectStatus.READY)

        with self._projects_lock:
            self._projects[project.id] = project

        return project

    def get_project(self, project_id: str) -> Project:
        with self._projects_lock:
            project = self._projects.get(project_id)
        if project is None:
            raise RedstoneAgentError(AgentErrorCode.PROJECT_NOT_FOUND)
        return project

    # ------------------------------------------------------------- leases

    def _lease_for(self, workspace_id: str) -> _WorkspaceLease:
        with self._leases_lock:
            lease = self._leases.get(workspace_id)
            if lease is None:
                lease = _WorkspaceLease()
                self._leases[workspace_id] = lease
            return lease

    # --------------------------------------------------------------- tasks

    def start_task(self, project_id: str, message: str, *, background: bool = False) -> AgentTask:
        """Create and run a task for `project_id`.

        `background=False` (the default, and what the HTTP API uses in Phase 3)
        runs the whole loop before returning — the caller gets the final task.
        `background=True` runs the loop on a daemon thread and returns
        immediately with the CREATED task, so `cancel_task` can interrupt it
        mid-run; used by tests that exercise real cancellation.

        Raises WORKSPACE_BUSY immediately (never blocks) if another task is
        already active for this project's workspace.
        """
        if not isinstance(message, str) or not message.strip():
            raise RedstoneAgentError(
                AgentErrorCode.INVALID_REQUEST, safe_message="Message must not be empty."
            )

        project = self.get_project(project_id)
        lease = self._lease_for(project.workspace_id)

        if not lease.lock.acquire(blocking=False):
            raise RedstoneAgentError(AgentErrorCode.WORKSPACE_BUSY)

        task = AgentTask.create(project.id, project.workspace_id, message)
        cancel_event = threading.Event()

        with self._tasks_lock:
            self._tasks[task.id] = task
            self._task_events[task.id] = []
            self._cancel_flags[task.id] = cancel_event
        lease.active_task_id = task.id

        def _finish(final_task: AgentTask) -> None:
            with self._tasks_lock:
                self._tasks[task.id] = final_task
            lease.active_task_id = None
            lease.lock.release()

        def _run() -> None:
            # The lease MUST be released no matter what happens below, or a
            # setup failure (before run_agent_task even starts, which already
            # guarantees a terminal AgentTask on its own) would leave this
            # workspace permanently WORKSPACE_BUSY.
            try:
                workspace = self._workspaces.get(project.workspace_id)
                context = ToolContext(
                    workspace=workspace,
                    limits=self._config.limits,
                    validation_runner=self._validation_runner,
                )
                snapshot_store = SnapshotStore(
                    workspace.root, workspace.project_root, self._config.limits
                )
                final_task = run_agent_task(
                    task,
                    gateway=self._gateway,
                    registry=self._registry,
                    context=context,
                    snapshot_store=snapshot_store,
                    limits=self._config.limits,
                    on_update=self._record_update,
                    on_event=self._record_event(project.id, task.id),
                    is_cancelled=cancel_event.is_set,
                )
            except Exception as exc:  # noqa: BLE001 - setup failure before the loop's own safety net
                final_task = task.with_status(AgentStatus.FAILED).with_error(
                    AgentErrorCode.INTERNAL_ERROR.value,
                    f"The agent task could not start ({type(exc).__name__}).",
                )
            _finish(final_task)

        if background:
            thread = threading.Thread(target=_run, daemon=True, name=f"redstone-agent-{task.id}")
            thread.start()
            return task

        _run()
        return self.get_task(task.id)

    def get_task(self, task_id: str) -> AgentTask:
        with self._tasks_lock:
            task = self._tasks.get(task_id)
        if task is None:
            raise RedstoneAgentError(AgentErrorCode.NOT_FOUND)
        return task

    def get_task_events(self, task_id: str) -> list[dict]:
        with self._tasks_lock:
            if task_id not in self._tasks:
                raise RedstoneAgentError(AgentErrorCode.NOT_FOUND)
            events = list(self._task_events.get(task_id, ()))
        return [event.to_dict() for event in events]

    def cancel_task(self, task_id: str) -> None:
        with self._tasks_lock:
            if task_id not in self._tasks:
                raise RedstoneAgentError(AgentErrorCode.NOT_FOUND)
            flag = self._cancel_flags.get(task_id)
        if flag is not None:
            flag.set()

    # -------------------------------------------------------------- helpers

    def _record_update(self, task: AgentTask) -> None:
        with self._tasks_lock:
            self._tasks[task.id] = task

    def _record_event(self, project_id: str, task_id: str):
        def _publish(event_type: EventType, payload: dict) -> None:
            event = self._events.publish(
                event_type, project_id=project_id, task_id=task_id, payload=payload
            )
            with self._tasks_lock:
                self._task_events.setdefault(task_id, []).append(event)

        return _publish
