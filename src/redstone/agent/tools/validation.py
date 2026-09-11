"""Validation tools: run_typecheck, run_lint, run_build.

These are the only "execution" surface the agent has, and deliberately a
narrow one: the AI chooses *which* fixed operation to run, never the command
itself. There is no `run_command(command)` tool, and none of the handlers in
this module accept a command, script name, or argument that would let a model
control what actually executes.

Phase 3 does not build the sandbox/runtime (Phase 4). Running a project's real
build/typecheck/lint script means executing that project's own scripts and
dependencies, which is exactly the untrusted execution Phase 4's isolation
boundary exists for. Running it here, on the trusted backend, before that
boundary exists, would defeat every guarantee in SECURITY.md.

So the runner used by default is `UnavailableValidationRunner`: it performs no
execution and returns an honest "unavailable" result, never a fabricated pass.
The `ValidationRunner` protocol is the seam a Phase 4 sandboxed runner plugs
into without changing this module, the registry, or the agent loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .specs import ToolContext, ToolSpec  # noqa: F401 (ToolContext used in type hints below)

__all__ = ["ValidationStatus", "ValidationResult", "ValidationRunner",
           "UnavailableValidationRunner", "TOOL_SPECS"]


class ValidationStatus:
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    status: str          # one of ValidationStatus
    message: str
    output: str = ""      # truncated tool output, if any was actually produced

    def to_dict(self) -> dict:
        return {"status": self.status, "message": self.message, "output": self.output}


class ValidationRunner(Protocol):
    """What a validation tool handler depends on. Phase 4 supplies a real
    implementation that runs inside sandbox isolation; nothing above this
    seam needs to change when that happens."""

    def run_typecheck(self, project_root: Path) -> ValidationResult: ...
    def run_lint(self, project_root: Path) -> ValidationResult: ...
    def run_build(self, project_root: Path) -> ValidationResult: ...


class UnavailableValidationRunner:
    """The only runner wired in by default. Never fabricates a result."""

    _MESSAGE = (
        "Validation is not available yet: running a project's real build, "
        "lint or typecheck script requires executing that project's own "
        "scripts and dependencies, which needs the Phase 4 sandbox/runtime "
        "isolation boundary. This is not a bug — it is the trust boundary "
        "documented in SECURITY.md."
    )

    def run_typecheck(self, project_root: Path) -> ValidationResult:
        return ValidationResult(ValidationStatus.UNAVAILABLE, self._MESSAGE)

    def run_lint(self, project_root: Path) -> ValidationResult:
        return ValidationResult(ValidationStatus.UNAVAILABLE, self._MESSAGE)

    def run_build(self, project_root: Path) -> ValidationResult:
        return ValidationResult(ValidationStatus.UNAVAILABLE, self._MESSAGE)


def _runner(context: "ToolContext") -> ValidationRunner:
    return context.validation_runner or UnavailableValidationRunner()


def _handle_typecheck(context: "ToolContext", arguments: dict) -> dict:
    return _runner(context).run_typecheck(context.workspace.project_root).to_dict()


def _handle_lint(context: "ToolContext", arguments: dict) -> dict:
    return _runner(context).run_lint(context.workspace.project_root).to_dict()


def _handle_build(context: "ToolContext", arguments: dict) -> dict:
    return _runner(context).run_build(context.workspace.project_root).to_dict()


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="run_typecheck",
        description="Run the project's type checker. Takes no arguments.",
        args={},
        handler=_handle_typecheck,
        timeout_seconds=120.0,
        kind="validate",
    ),
    ToolSpec(
        name="run_lint",
        description="Run the project's linter. Takes no arguments.",
        args={},
        handler=_handle_lint,
        timeout_seconds=60.0,
        kind="validate",
    ),
    ToolSpec(
        name="run_build",
        description="Run the project's build. Takes no arguments.",
        args={},
        handler=_handle_build,
        timeout_seconds=180.0,
        kind="validate",
    ),
)
