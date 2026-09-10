"""Domain models.

Framework-independent by design: no FastAPI, no pydantic, no HTTP. These types
are what the services, the agent and the runtime pass between each other, so
they must be usable from a test, a CLI, or a background worker without a web
server present.

Everything is a frozen dataclass. Dictionaries flowing through a backend are
how fields get silently renamed, misspelled, or leaked.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum

__all__ = [
    "ProjectStatus", "RuntimeState", "ChangeKind", "EventType", "Framework",
    "Project", "FileEntry", "FileChange", "ChangeSet", "Snapshot",
    "Runtime", "Preview", "ErrorModel", "new_id", "utcnow",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    """Short, collision-resistant, and never derived from a secret."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Framework(str, Enum):
    """Supported project stacks.

    STATIC is the zero-dependency starter used to prove the build/run/preview
    loop in environments without package-registry access. REACT_VITE_TS is the
    intended primary stack.
    """

    STATIC = "static"
    REACT_VITE_TS = "react-vite-ts"


class ProjectStatus(str, Enum):
    CREATED = "created"
    INITIALIZING = "initializing"
    READY = "ready"
    BUILDING = "building"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    EXPIRED = "expired"


class RuntimeState(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    IDLE = "idle"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    EXPIRED = "expired"


class ChangeKind(str, Enum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class EventType(str, Enum):
    AGENT_STARTED = "agent.started"
    AGENT_TOOL_STARTED = "agent.tool_started"
    AGENT_TOOL_COMPLETED = "agent.tool_completed"
    AGENT_COMPLETED = "agent.completed"
    AGENT_FAILED = "agent.failed"
    FILE_CHANGED = "file.changed"
    BUILD_STARTED = "build.started"
    BUILD_COMPLETED = "build.completed"
    RUNTIME_STARTED = "runtime.started"
    RUNTIME_STOPPED = "runtime.stopped"
    RUNTIME_ERROR = "runtime.error"
    PREVIEW_STARTED = "preview.started"
    PREVIEW_READY = "preview.ready"
    PREVIEW_FAILED = "preview.failed"
    SNAPSHOT_CREATED = "snapshot.created"
    SNAPSHOT_RESTORED = "snapshot.restored"


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    framework: Framework
    workspace_id: str
    status: ProjectStatus = ProjectStatus.CREATED
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @classmethod
    def create(cls, name: str, framework: Framework = Framework.STATIC) -> Project:
        return cls(
            id=new_id("prj"),
            name=name,
            framework=framework,
            workspace_id=new_id("ws"),
        )

    def with_status(self, status: ProjectStatus) -> Project:
        return replace(self, status=status, updated_at=utcnow())


@dataclass(frozen=True, slots=True)
class FileEntry:
    """A file or directory, described by its workspace-relative path.

    Absolute host paths never appear here: they would disclose server layout
    through the API and through AI prompts.
    """

    path: str
    is_directory: bool
    size: int = 0
    modified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    kind: ChangeKind
    additions: int = 0
    deletions: int = 0
    previous_path: str | None = None
    before_hash: str | None = None
    after_hash: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """What actually changed on disk during one agent task.

    Built by comparing real filesystem state before and after, never from a
    model's description of what it intended to do.
    """

    id: str
    project_id: str
    task_id: str | None
    changes: tuple[FileChange, ...]
    reason: str = ""
    created_at: datetime = field(default_factory=utcnow)

    @property
    def is_empty(self) -> bool:
        return not self.changes

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(change.path for change in self.changes)

    def of_kind(self, kind: ChangeKind) -> tuple[FileChange, ...]:
        return tuple(change for change in self.changes if change.kind is kind)


@dataclass(frozen=True, slots=True)
class Snapshot:
    id: str
    project_id: str
    label: str = ""
    file_count: int = 0
    total_bytes: int = 0
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class Runtime:
    id: str
    project_id: str
    state: RuntimeState = RuntimeState.CREATED
    port: int | None = None
    created_at: datetime = field(default_factory=utcnow)
    last_activity: datetime = field(default_factory=utcnow)

    def with_state(self, state: RuntimeState) -> Runtime:
        return replace(self, state=state, last_activity=utcnow())


@dataclass(frozen=True, slots=True)
class Preview:
    """How a running project is reached.

    `path` is a Redstone-controlled route, not a host port. Users never address
    a runtime's real port directly.
    """

    project_id: str
    runtime_id: str
    path: str
    ready: bool = False


@dataclass(frozen=True, slots=True)
class ErrorModel:
    """The single error shape crossing every boundary.

    `safe_message` is written by Redstone. Upstream exception text, provider
    responses and host paths never populate it.
    """

    error_code: str
    safe_message: str
    request_id: str | None = None
    retryable: bool = False

    def to_dict(self) -> dict:
        return {
            "error_code": self.error_code,
            "error": self.safe_message,
            "request_id": self.request_id,
            "retryable": self.retryable,
        }
