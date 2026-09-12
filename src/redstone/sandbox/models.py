"""Sandbox domain model: the provider-neutral shape every SandboxProvider
implements against.

Nothing here is Docker-shaped. A config is built from explicit fields (never
"inherit the host"), and a provider translates it into whatever its backend
actually needs (Docker flags, a subprocess argv list, a future Firecracker
spec). Adding a provider must never require changing this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .commands import SandboxCommand

__all__ = [
    "SandboxState",
    "NetworkPolicy",
    "ResourceLimits",
    "Mount",
    "SandboxConfig",
    "SandboxStatus",
    "SandboxResult",
    "TERMINAL_SANDBOX_STATES",
]


class SandboxState(str, Enum):
    """The lifecycle of ONE sandboxed execution (one container run).

    Distinct from redstone.runtime.models.RuntimeState, which is the
    higher-level, public dev-server lifecycle RuntimeManager exposes -- a
    single logical Runtime spans at least two SandboxState lifecycles (an
    install execution, then a run execution).
    """

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    KILLED = "killed"
    DESTROYED = "destroyed"


TERMINAL_SANDBOX_STATES = frozenset(
    {SandboxState.STOPPED, SandboxState.FAILED, SandboxState.KILLED, SandboxState.DESTROYED}
)


class NetworkPolicy(str, Enum):
    """What network access a sandboxed execution gets.

    Only DENY and INSTALL_ONLY are implemented in Phase 4. ALLOWLIST and FULL
    are declared so the architecture does not need to change shape when they
    are built, but selecting either today raises UNSUPPORTED_OPERATION rather
    than silently falling back to something less restrictive.
    """

    DENY = "deny"                # no network device at all (Docker --network none)
    INSTALL_ONLY = "install_only"  # outbound-only, no published ports; dependency install
    ALLOWLIST = "allowlist"       # NOT IMPLEMENTED: needs an egress proxy
    FULL = "full"                 # NOT IMPLEMENTED, and deliberately never the default


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Requested ceilings. A provider that cannot enforce one of these at the
    OS/container level must say so via SandboxStatus.enforced_limits rather
    than silently accepting and ignoring it."""

    cpu_cores: float = 1.0
    memory_mb: int = 512
    pids: int = 128
    timeout_seconds: float = 120.0
    output_bytes: int = 256 * 1024


@dataclass(frozen=True, slots=True)
class Mount:
    """A single bind mount. `read_only=True` unless the sandbox needs to
    write there -- the project workspace is the one mount that is not."""

    host_path: Path
    container_path: str
    read_only: bool = True


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Everything one sandboxed execution needs, and nothing it can infer.

    `environment` is the COMPLETE environment the process receives. There is
    no flag to inherit the host's; a provider that did that anyway would be a
    bug, and it is exactly the property Phase 4's tests check for directly.
    """

    mounts: tuple[Mount, ...]
    working_dir: str
    command: SandboxCommand
    environment: dict[str, str] = field(default_factory=dict)
    network_policy: NetworkPolicy = NetworkPolicy.DENY
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    read_only_root: bool = True
    tmpfs_paths: tuple[str, ...] = ("/tmp",)
    # None means "the provider's own safe default" (e.g. a fixed non-root uid
    # inside the sandbox image), never "inherit the caller's identity".
    user: str | None = None

    def __post_init__(self) -> None:
        if not self.mounts:
            raise ValueError("a sandbox needs at least one mount")
        if self.network_policy in (NetworkPolicy.ALLOWLIST, NetworkPolicy.FULL):
            # Constructing a config with an unimplemented policy is itself a
            # mistake worth catching immediately, not just at execution time.
            raise ValueError(f"{self.network_policy.value} is not implemented in Phase 4")


@dataclass(frozen=True, slots=True)
class SandboxStatus:
    sandbox_id: str
    state: SandboxState
    exit_code: int | None = None
    enforced_limits: tuple[str, ...] = ()   # which ResourceLimits fields are actually enforced


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """The outcome of one execute() call."""

    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    duration_seconds: float

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0
