"""Structured trusted operations.

The AI, and any caller above this module, chooses one of a fixed set of named
operations -- never a command string. `SandboxCommand.resolve()` is the only
place an operation becomes an actual argv, and it does so from a fixed lookup
table keyed by (Framework, Operation). There is no code path from user input,
agent output, or project content to an arbitrary executable or argument list.

Phase 4 supports exactly one generated stack: React + Vite + TypeScript. Any
other framework raises UNSUPPORTED_OPERATION rather than guessing a command.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..domain.models import Framework
from .errors import RedstoneSandboxError, SandboxErrorCode

__all__ = ["Operation", "SandboxCommand"]


class Operation(str, Enum):
    INSTALL_DEPENDENCIES = "install_dependencies"
    TYPECHECK = "typecheck"
    LINT = "lint"
    BUILD = "build"
    START_DEV_SERVER = "start_dev_server"


# Fixed argv per (framework, operation). Never string-formatted from external
# input -- every element here is a literal written by Redstone.
_TABLE: dict[Framework, dict[Operation, tuple[str, ...]]] = {
    Framework.REACT_VITE_TS: {
        Operation.INSTALL_DEPENDENCIES: ("npm", "install", "--no-audit", "--no-fund"),
        Operation.TYPECHECK: ("npx", "--no-install", "tsc", "--noEmit"),
        Operation.LINT: ("npx", "--no-install", "eslint", "."),
        Operation.BUILD: ("npm", "run", "build", "--if-present"),
        Operation.START_DEV_SERVER: (
            "npm", "run", "dev", "--",
            "--port", "5173", "--strictPort", "--host", "127.0.0.1",
        ),
    },
}

# Which operations need outbound network access (dependency resolution).
# Everything else -- including running the dev server -- gets NetworkPolicy.DENY.
NETWORK_REQUIRED_OPERATIONS = frozenset({Operation.INSTALL_DEPENDENCIES})

# A conservative upper bound per operation. The caller's ResourceLimits can
# always be tighter; this is what stops a caller from setting an unreasonably
# generous timeout for, say, a typecheck.
MAX_TIMEOUT_SECONDS: dict[Operation, float] = {
    Operation.INSTALL_DEPENDENCIES: 300.0,
    Operation.TYPECHECK: 120.0,
    Operation.LINT: 60.0,
    Operation.BUILD: 180.0,
    Operation.START_DEV_SERVER: 30.0,   # time to become healthy, not the server's lifetime
}


@dataclass(frozen=True, slots=True)
class SandboxCommand:
    operation: Operation
    framework: Framework

    def resolve(self) -> tuple[str, ...]:
        table = _TABLE.get(self.framework)
        if table is None or self.operation not in table:
            raise RedstoneSandboxError(
                SandboxErrorCode.UNSUPPORTED_OPERATION,
                safe_message=(
                    f"'{self.operation.value}' is not supported for framework "
                    f"'{self.framework.value}' in this version of Redstone."
                ),
            )
        return table[self.operation]

    @property
    def needs_network(self) -> bool:
        return self.operation in NETWORK_REQUIRED_OPERATIONS

    @property
    def max_timeout_seconds(self) -> float:
        return MAX_TIMEOUT_SECONDS.get(self.operation, 60.0)
