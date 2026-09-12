"""Redstone sandbox layer: execute untrusted generated projects behind a real
trust boundary.

`redstone.runtime` (Phase 5) depends only on `SandboxProvider` and the neutral
models exported here -- never on Docker directly. See docs/redstone/SANDBOX.md
for the full threat model and docs/redstone/RUNTIME.md for how RuntimeManager
uses this layer.
"""

from .commands import Operation, SandboxCommand
from .errors import RedstoneSandboxError, SandboxErrorCode
from .models import (
    Mount,
    NetworkPolicy,
    ResourceLimits,
    SandboxConfig,
    SandboxResult,
    SandboxState,
    SandboxStatus,
    TERMINAL_SANDBOX_STATES,
)
from .provider import SandboxProvider

__all__ = [
    "SandboxProvider",
    "SandboxConfig",
    "SandboxStatus",
    "SandboxResult",
    "SandboxState",
    "TERMINAL_SANDBOX_STATES",
    "ResourceLimits",
    "NetworkPolicy",
    "Mount",
    "Operation",
    "SandboxCommand",
    "SandboxErrorCode",
    "RedstoneSandboxError",
]
