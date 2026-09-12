"""SandboxValidationRunner: the Phase 4 implementation the Phase 3
ValidationRunner seam was built for.

redstone.agent.tools.validation.ValidationRunner is a Protocol (structural,
not inherited), so this class needs no import from the agent package -- it
just needs to match the same three-method shape. That keeps the dependency
direction correct: agent depends on runtime/sandbox at wiring time (the
composition root passes a SandboxValidationRunner into AgentService), never
the other way around.

Each call is one bounded sandboxed execution -- create -> start -> wait ->
destroy -- exactly the same shape RuntimeManager._run_install uses for
dependency installation, and for the same reason: typecheck/lint/build are
untrusted project scripts, never executed on the trusted host.

This does NOT install dependencies first. If a project's node_modules is
missing, tsc/eslint/npm-run-build will fail with a real, honest error --
exactly what should happen; silently installing here would duplicate
RuntimeManager's own install/network handling and risk two independent
processes fighting over the same node_modules directory.
"""

from __future__ import annotations

from pathlib import Path

from ..agent.tools.validation import ValidationResult, ValidationStatus
from ..domain.models import Framework
from ..sandbox.commands import Operation, SandboxCommand
from ..sandbox.errors import RedstoneSandboxError
from ..sandbox.models import Mount, NetworkPolicy, ResourceLimits, SandboxConfig
from ..sandbox.provider import SandboxProvider

__all__ = ["SandboxValidationRunner"]

_CONTAINER_PROJECT_PATH = "/workspace/project"
_MAX_OUTPUT_CHARS = 8000


class SandboxValidationRunner:
    """Runs typecheck/lint/build inside the sandbox. No new tool surface: the
    agent still only ever picks one of the three fixed ToolSpecs in
    agent/tools/validation.py, which is what calls into this class."""

    def __init__(
        self,
        provider: SandboxProvider,
        *,
        framework: Framework = Framework.REACT_VITE_TS,
        resource_limits: ResourceLimits | None = None,
    ) -> None:
        self._provider = provider
        self._framework = framework
        self._resource_limits = resource_limits or ResourceLimits()

    def run_typecheck(self, project_root: Path) -> ValidationResult:
        return self._run(Operation.TYPECHECK, project_root)

    def run_lint(self, project_root: Path) -> ValidationResult:
        return self._run(Operation.LINT, project_root)

    def run_build(self, project_root: Path) -> ValidationResult:
        return self._run(Operation.BUILD, project_root)

    def _run(self, operation: Operation, project_root: Path) -> ValidationResult:
        command = SandboxCommand(operation, self._framework)
        limits = ResourceLimits(
            cpu_cores=self._resource_limits.cpu_cores,
            memory_mb=self._resource_limits.memory_mb,
            pids=self._resource_limits.pids,
            timeout_seconds=command.max_timeout_seconds,
            output_bytes=self._resource_limits.output_bytes,
        )
        config = SandboxConfig(
            mounts=(Mount(project_root, _CONTAINER_PROJECT_PATH, read_only=False),),
            working_dir=_CONTAINER_PROJECT_PATH,
            command=command,
            environment={"NODE_ENV": "development"},
            # None of typecheck/lint/build need network -- they operate on
            # already-installed node_modules.
            network_policy=NetworkPolicy.DENY,
            resource_limits=limits,
        )

        try:
            sandbox_id = self._provider.create(config)
        except RedstoneSandboxError as exc:
            return ValidationResult(ValidationStatus.UNAVAILABLE, exc.safe_message)

        try:
            self._provider.start(sandbox_id)
            result = self._provider.wait(sandbox_id, timeout=command.max_timeout_seconds)
        except RedstoneSandboxError as exc:
            return ValidationResult(ValidationStatus.FAILED, exc.safe_message)
        finally:
            self._provider.destroy(sandbox_id)

        output = (result.stdout + result.stderr)[-_MAX_OUTPUT_CHARS:]
        if result.timed_out:
            return ValidationResult(
                ValidationStatus.FAILED, f"{operation.value} timed out.", output
            )
        if result.ok:
            return ValidationResult(
                ValidationStatus.PASSED, f"{operation.value} passed.", output
            )
        return ValidationResult(
            ValidationStatus.FAILED, f"{operation.value} failed.", output
        )
