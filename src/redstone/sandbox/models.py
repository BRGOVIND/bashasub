"""Sandbox domain model: the provider-neutral shape every SandboxProvider
implements against.

Nothing here is Docker-shaped. A config is built from explicit fields (never
"inherit the host"), and a provider translates it into whatever its backend
actually needs (Docker flags, a subprocess argv list, a future Firecracker
spec). Adding a provider must never require changing this module.
"""

from __future__ import annotations

import ipaddress
import re
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
    # Dependency install: an --internal network whose only peer is
    # Redstone's egress proxy, which allows CONNECT to InstallEgressPolicy
    # hosts and nothing else (Phase 4.2). No direct route anywhere.
    INSTALL_ONLY = "install_only"
    # Live preview (Phase 6): an --internal network whose only other member
    # is this preview's token-checking relay. No route out, no published port
    # on the app itself; reachable only as the relay's fixed upstream.
    PREVIEW = "preview"
    ALLOWLIST = "allowlist"       # NOT IMPLEMENTED: needs an egress proxy
    FULL = "full"                 # NOT IMPLEMENTED, and deliberately never the default


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Requested ceilings. A provider that cannot enforce one of these at the
    OS/container level must say so via SandboxStatus.enforced_limits rather
    than silently accepting and ignoring it.

    `storage_mb` is different in kind from the other four: a bind-mounted
    directory cannot be size-capped by any Docker container flag (Docker's
    own `--storage-opt size=` only bounds a container's own copy-on-write
    layer, never a bind mount -- this was verified against this exact daemon,
    not assumed). DockerSandboxProvider enforces it instead with an active
    polling watchdog that measures real on-disk usage of the mounted
    directory and kills the sandbox once it's exceeded. This is real
    enforcement (the untrusted process's own direct writes are what's being
    measured, not anything routed through Redstone's Python file-write path)
    but it is PERIODICALLY ENFORCED, not kernel-instantaneous like memory/pids: a
    fast writer can overshoot the ceiling by up to one poll interval's worth
    of writes before being killed. See docs/redstone/SANDBOX.md.
    """

    cpu_cores: float = 1.0
    memory_mb: int = 512
    pids: int = 128
    timeout_seconds: float = 120.0
    output_bytes: int = 256 * 1024
    storage_mb: int = 1024


@dataclass(frozen=True, slots=True)
class Mount:
    """A single bind mount. `read_only=True` unless the sandbox needs to
    write there -- the project workspace is the one mount that is not."""

    host_path: Path
    container_path: str
    read_only: bool = True


RELAY_TOKEN_HEADER = "x-redstone-relay-token"
RELAY_STATUS_HEADER = "x-redstone-relay"


@dataclass(frozen=True, slots=True)
class PreviewUpstream:
    """Where Redstone's preview gateway reaches one preview: the relay's
    loopback-published port and the secret it demands. Internal only --
    never serialised to an API response, an event or a log."""

    host: str
    port: int
    token: str = field(repr=False)


_REGISTRY_HOST = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


def validate_registry_host(host: str) -> str:
    """A plain DNS name, normalised. Rejects IP literals, numeric-looking
    names (inet_aton accepts forms like '127.1' or '0x7f.1'), wildcards,
    userinfo, ports and paths -- an allowlist entry is exactly one host."""
    if not isinstance(host, str):
        raise ValueError("registry host must be a string")
    candidate = host.strip().lower()
    if candidate.endswith("."):
        candidate = candidate[:-1]
    if not _REGISTRY_HOST.match(candidate):
        raise ValueError(f"invalid registry host: {host!r}")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        raise ValueError("registry host must be a name, not an IP address")
    if not re.search(r"[a-z]", candidate.rsplit(".", 1)[-1]):
        raise ValueError("registry host's top-level label must contain a letter")
    return candidate


@dataclass(frozen=True, slots=True)
class InstallEgressPolicy:
    """What an INSTALL_ONLY sandbox may reach -- and nothing else.

    Enforced by Redstone's egress proxy (sandbox/egress_proxy.js), which is
    the ONLY route out of an install sandbox's --internal network. The
    default allowlist is the single host npm needs for metadata and
    tarballs; every additional host an operator configures is additional
    reachable surface for arbitrary lifecycle-script code.
    """

    allowed_hosts: tuple[str, ...] = ("registry.npmjs.org",)
    allowed_ports: tuple[int, ...] = (443,)
    connect_timeout_seconds: float = 10.0
    idle_timeout_seconds: float = 60.0
    max_tunnel_seconds: float = 600.0
    max_connections: int = 32
    startup_timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if not self.allowed_hosts or len(self.allowed_hosts) > 16:
            raise ValueError("allowed_hosts must list 1-16 hosts")
        object.__setattr__(
            self, "allowed_hosts", tuple(validate_registry_host(h) for h in self.allowed_hosts)
        )
        if not self.allowed_ports or any(
            not isinstance(p, int) or isinstance(p, bool) or not 1 <= p <= 65535
            for p in self.allowed_ports
        ):
            raise ValueError("allowed_ports must be integers in 1-65535")
        bounds = (
            (self.connect_timeout_seconds, 0.5, 120.0, "connect_timeout_seconds"),
            (self.idle_timeout_seconds, 1.0, 600.0, "idle_timeout_seconds"),
            (self.max_tunnel_seconds, 1.0, 3600.0, "max_tunnel_seconds"),
            (self.max_connections, 1, 1024, "max_connections"),
            (self.startup_timeout_seconds, 1.0, 120.0, "startup_timeout_seconds"),
        )
        for value, low, high, name in bounds:
            if not low <= value <= high:
                raise ValueError(f"{name} must be within [{low}, {high}]")

    @property
    def registry_url(self) -> str:
        return f"https://{self.allowed_hosts[0]}/"


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
    # Ownership metadata for orphan reconciliation across a Redstone process
    # restart (Phase 4.1/5.1). Opaque ids only -- never a secret. A provider
    # additionally stamps its own "this is Redstone's" marker unconditionally;
    # this dict carries the caller's own project/workspace/runtime ids on top.
    labels: dict[str, str] = field(default_factory=dict)
    # Only consulted for NetworkPolicy.INSTALL_ONLY. Defaults to the
    # restrictive npm-registry-only policy.
    egress: InstallEgressPolicy = field(default_factory=InstallEgressPolicy)

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
    # Which ResourceLimits field caused this sandbox to be killed, if any --
    # e.g. "storage_mb" when the disk-quota watchdog fired. None means either
    # still running, or terminated for a reason other than a resource limit.
    resource_limit_exceeded: str | None = None


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """The outcome of one execute() call."""

    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    duration_seconds: float
    # Same meaning as SandboxStatus.resource_limit_exceeded -- populated when
    # this bounded operation was killed by a resource-limit watchdog rather
    # than exiting on its own or hitting the operation's own timeout.
    resource_limit_exceeded: str | None = None

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0
