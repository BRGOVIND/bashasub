"""RuntimeManager: the trusted orchestration layer for sandbox lifecycles.

Depends only on `redstone.sandbox.SandboxProvider` and the neutral models --
never on Docker, subprocess, or any provider-specific type. RuntimeManager
itself is not a shell executor: every command a sandbox runs comes from
`SandboxCommand.resolve()`'s fixed table, never a string RuntimeManager built.

Two locks, two different jobs, matching the distinction already established in
redstone.agent.service:
  * `self._lock` guards the manager's own bookkeeping dicts (short critical
    sections only);
  * a per-workspace *admission* slot (`self._workspace_active`) enforces one
    active runtime per workspace, checked/set atomically under `self._lock`;
  * a per-runtime *operation* lock (`self._op_locks[runtime_id]`) serialises
    start/stop/restart/destroy on the SAME runtime, so concurrent calls
    cannot interleave and corrupt state.
"""

from __future__ import annotations

import logging
import threading
import time

from ..domain.models import EventType, Framework, Runtime, RuntimeState, new_id
from ..events.bus import EventBus
from ..sandbox.commands import Operation, SandboxCommand
from ..sandbox.errors import RedstoneSandboxError
from ..sandbox.models import Mount, NetworkPolicy, ResourceLimits, SandboxConfig, SandboxState
from ..sandbox.provider import SandboxProvider
from ..workspace.manager import Workspace
from .errors import RedstoneRuntimeError, RuntimeErrorCode
from .models import TERMINAL_RUNTIME_STATES, transition

__all__ = ["RuntimeManager"]

_CONTAINER_PROJECT_PATH = "/workspace/project"
_DEV_SERVER_PORT = 5173


class RuntimeManager:
    def __init__(
        self,
        provider: SandboxProvider,
        *,
        max_startup_seconds: float = 60.0,
        health_check_timeout: float = 5.0,
        stop_grace_seconds: float = 10.0,
        health_poll_interval: float = 1.0,
        resource_limits: ResourceLimits | None = None,
        event_bus: EventBus | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._provider = provider
        self._max_startup_seconds = max_startup_seconds
        self._health_check_timeout = health_check_timeout
        self._stop_grace_seconds = stop_grace_seconds
        self._health_poll_interval = health_poll_interval
        self._resource_limits = resource_limits or ResourceLimits()
        self._events = event_bus
        self._logger = logger

        self._lock = threading.Lock()
        self._runtimes: dict[str, Runtime] = {}
        self._workspace_active: dict[str, str] = {}   # workspace_id -> runtime_id
        self._op_locks: dict[str, threading.Lock] = {}

    # ------------------------------------------------------------- lookup

    def _op_lock(self, runtime_id: str) -> threading.Lock:
        with self._lock:
            lock = self._op_locks.get(runtime_id)
            if lock is None:
                lock = threading.Lock()
                self._op_locks[runtime_id] = lock
            return lock

    def _get(self, runtime_id: str) -> Runtime:
        with self._lock:
            runtime = self._runtimes.get(runtime_id)
        if runtime is None:
            raise RedstoneRuntimeError(RuntimeErrorCode.NOT_FOUND)
        return runtime

    def _get_owned(self, runtime_id: str, project_id: str) -> Runtime:
        runtime = self._get(runtime_id)
        # Ownership is checked against server-side state recorded at create()
        # time, never against anything the caller merely asserts.
        if runtime.project_id != project_id:
            raise RedstoneRuntimeError(RuntimeErrorCode.NOT_OWNED)
        return runtime

    def _save(self, runtime: Runtime) -> Runtime:
        with self._lock:
            self._runtimes[runtime.id] = runtime
        return runtime

    def _release_slot(self, workspace_id: str, runtime_id: str) -> None:
        with self._lock:
            if self._workspace_active.get(workspace_id) == runtime_id:
                del self._workspace_active[workspace_id]

    def _emit(self, event_type: EventType, runtime_id: str, *, project_id: str | None = None,
              **payload) -> None:
        if self._events is not None:
            self._events.publish(
                event_type, project_id=project_id,
                payload={"runtime_id": runtime_id, **payload},
            )
        if self._logger is not None:
            self._logger.info(f"runtime_event type={event_type.value} runtime_id={runtime_id}")

    @property
    def provider(self) -> SandboxProvider:
        """Read-only introspection (e.g. an honest health endpoint reporting
        whether the active provider actually provides isolation). Never used
        internally in place of self._provider -- this exists for callers
        outside the class."""
        return self._provider

    def get(self, runtime_id: str, project_id: str) -> Runtime:
        return self._get_owned(runtime_id, project_id)

    def list_for_project(self, project_id: str) -> tuple[Runtime, ...]:
        with self._lock:
            return tuple(r for r in self._runtimes.values() if r.project_id == project_id)

    # -------------------------------------------------------------- create

    def create(self, project_id: str, workspace: Workspace, framework: Framework) -> Runtime:
        with self._lock:
            existing = self._workspace_active.get(workspace.id)
            if existing is not None:
                raise RedstoneRuntimeError(RuntimeErrorCode.BUSY)
            runtime = Runtime(
                id=new_id("rt"), project_id=project_id, workspace_id=workspace.id,
                framework=framework,
            )
            self._runtimes[runtime.id] = runtime
            self._workspace_active[workspace.id] = runtime.id

        self._emit(EventType.RUNTIME_CREATED, runtime.id, project_id=project_id)
        return runtime

    # --------------------------------------------------------------- start

    def start(self, runtime_id: str, project_id: str, workspace: Workspace) -> Runtime:
        with self._op_lock(runtime_id):
            runtime = self._get_owned(runtime_id, project_id)

            if runtime.state is RuntimeState.RUNNING:
                return runtime   # idempotent: starting a running runtime is a no-op

            runtime = self._save(transition(runtime, RuntimeState.STARTING))
            self._emit(EventType.RUNTIME_STARTING, runtime.id)

            try:
                if self._needs_dependencies(workspace):
                    self._run_install(runtime, workspace)

                sandbox_id = self._start_dev_server(runtime, workspace)
                runtime = self._save(runtime.with_sandbox(sandbox_id, self._provider.name))

                if not self._wait_until_healthy(sandbox_id):
                    self._safe_teardown(sandbox_id)
                    raise RedstoneRuntimeError(RuntimeErrorCode.HEALTHCHECK_FAILED)

            except RedstoneRuntimeError as exc:
                runtime = self._save(
                    transition(runtime, RuntimeState.FAILED).with_error(exc.code.value, exc.safe_message)
                )
                self._release_slot(runtime.workspace_id, runtime.id)
                self._emit(EventType.RUNTIME_FAILED, runtime.id, error_code=exc.code.value)
                raise
            except RedstoneSandboxError as exc:
                wrapped = RedstoneRuntimeError(RuntimeErrorCode.START_FAILED,
                                              safe_message=exc.safe_message, internal=exc.internal)
                runtime = self._save(
                    transition(runtime, RuntimeState.FAILED).with_error(
                        RuntimeErrorCode.START_FAILED.value, exc.safe_message
                    )
                )
                self._release_slot(runtime.workspace_id, runtime.id)
                self._emit(EventType.RUNTIME_FAILED, runtime.id, error_code=exc.code.value)
                raise wrapped from exc

            runtime = self._save(transition(runtime, RuntimeState.RUNNING))
            self._emit(EventType.RUNTIME_STARTED, runtime.id)
            return runtime

    def _needs_dependencies(self, workspace: Workspace) -> bool:
        return (workspace.project_root / "package.json").is_file()

    def _run_install(self, runtime: Runtime, workspace: Workspace) -> None:
        command = SandboxCommand(Operation.INSTALL_DEPENDENCIES, runtime.framework)
        config = self._build_config(
            workspace, command,
            network_policy=NetworkPolicy.INSTALL_ONLY if command.needs_network else NetworkPolicy.DENY,
            timeout_seconds=command.max_timeout_seconds,
        )
        sandbox_id = self._provider.create(config)
        try:
            self._provider.start(sandbox_id)
            result = self._provider.wait(sandbox_id, timeout=command.max_timeout_seconds)
        finally:
            self._provider.destroy(sandbox_id)

        if not result.ok:
            raise RedstoneRuntimeError(
                RuntimeErrorCode.START_FAILED,
                safe_message="Dependency installation failed.",
                internal=result.stdout[-2000:],
            )

    def _start_dev_server(self, runtime: Runtime, workspace: Workspace) -> str:
        command = SandboxCommand(Operation.START_DEV_SERVER, runtime.framework)
        config = self._build_config(
            workspace, command, network_policy=NetworkPolicy.DENY,
            timeout_seconds=self._resource_limits.timeout_seconds,
        )
        sandbox_id = self._provider.create(config)
        self._provider.start(sandbox_id)
        return sandbox_id

    def _build_config(
        self, workspace: Workspace, command: SandboxCommand, *,
        network_policy: NetworkPolicy, timeout_seconds: float,
    ) -> SandboxConfig:
        limits = ResourceLimits(
            cpu_cores=self._resource_limits.cpu_cores,
            memory_mb=self._resource_limits.memory_mb,
            pids=self._resource_limits.pids,
            timeout_seconds=timeout_seconds,
            output_bytes=self._resource_limits.output_bytes,
        )
        return SandboxConfig(
            mounts=(Mount(workspace.project_root, _CONTAINER_PROJECT_PATH, read_only=False),),
            working_dir=_CONTAINER_PROJECT_PATH,
            command=command,
            # The complete environment. Nothing from AIConfig, nothing from
            # os.environ -- RuntimeManager has no reference to either.
            environment={"NODE_ENV": "development", "PORT": str(_DEV_SERVER_PORT)},
            network_policy=network_policy,
            resource_limits=limits,
        )

    def _wait_until_healthy(self, sandbox_id: str) -> bool:
        deadline = time.monotonic() + self._max_startup_seconds
        probe = ("wget", "-T", "2", "-q", "-O", "-", f"http://127.0.0.1:{_DEV_SERVER_PORT}")

        while time.monotonic() < deadline:
            status = self._provider.status(sandbox_id)
            if status.state not in (SandboxState.RUNNING, SandboxState.CREATED, SandboxState.STARTING):
                return False   # crashed during startup

            result = self._provider.exec_in(sandbox_id, probe, timeout=self._health_check_timeout)
            if result.exit_code == 0:
                return True
            time.sleep(self._health_poll_interval)

        return False

    def _safe_teardown(self, sandbox_id: str) -> None:
        try:
            self._provider.kill(sandbox_id)
        finally:
            self._provider.destroy(sandbox_id)

    # ---------------------------------------------------------- health check

    def health_check(self, runtime_id: str, project_id: str) -> bool:
        """On-demand crash detection for an already-RUNNING runtime.

        Distinguishes "process exists" (sandbox state) from "server
        responding" (the probe) -- a container can be alive with a hung
        server, which this correctly reports as unhealthy.
        """
        runtime = self._get_owned(runtime_id, project_id)
        if runtime.state is not RuntimeState.RUNNING or runtime.sandbox_id is None:
            return False

        status = self._provider.status(runtime.sandbox_id)
        self._emit(EventType.RUNTIME_HEALTH_CHECK, runtime.id, sandbox_state=status.state.value)

        if status.state is not SandboxState.RUNNING:
            self._mark_crashed(runtime)
            return False

        probe = ("wget", "-T", "2", "-q", "-O", "-", f"http://127.0.0.1:{_DEV_SERVER_PORT}")
        result = self._provider.exec_in(runtime.sandbox_id, probe, timeout=self._health_check_timeout)
        if result.exit_code != 0:
            self._mark_crashed(runtime)
            return False
        return True

    def _mark_crashed(self, runtime: Runtime) -> None:
        with self._op_lock(runtime.id):
            current = self._get(runtime.id)
            if current.state in TERMINAL_RUNTIME_STATES:
                return   # already handled by a concurrent stop/destroy
            diagnostics = ""
            if current.sandbox_id:
                try:
                    diagnostics = self._provider.logs(current.sandbox_id, max_bytes=4096)
                except RedstoneSandboxError:
                    pass
            updated = self._save(
                transition(current, RuntimeState.FAILED).with_error(
                    RuntimeErrorCode.HEALTHCHECK_FAILED.value,
                    "The runtime process is no longer responding.",
                )
            )
            self._release_slot(updated.workspace_id, updated.id)
            self._emit(EventType.RUNTIME_CRASHED, updated.id, diagnostics_bytes=len(diagnostics))

    # ----------------------------------------------------------------- logs

    def logs(self, runtime_id: str, project_id: str, max_bytes: int = 64 * 1024) -> str:
        runtime = self._get_owned(runtime_id, project_id)
        if runtime.sandbox_id is None:
            return ""
        return self._provider.logs(runtime.sandbox_id, max_bytes)

    # ----------------------------------------------------------------- stop

    def stop(self, runtime_id: str, project_id: str) -> Runtime:
        with self._op_lock(runtime_id):
            runtime = self._get_owned(runtime_id, project_id)
            if runtime.state in TERMINAL_RUNTIME_STATES:
                return runtime   # idempotent

            runtime = self._save(transition(runtime, RuntimeState.STOPPING))
            self._emit(EventType.RUNTIME_STOPPING, runtime.id)

            if runtime.sandbox_id:
                try:
                    self._provider.stop(runtime.sandbox_id, timeout=self._stop_grace_seconds)
                except RedstoneSandboxError as exc:
                    raise RedstoneRuntimeError(RuntimeErrorCode.STOP_FAILED,
                                               safe_message=exc.safe_message) from exc

            runtime = self._save(transition(runtime, RuntimeState.STOPPED))
            self._release_slot(runtime.workspace_id, runtime.id)
            self._emit(EventType.RUNTIME_STOPPED, runtime.id)
            return runtime

    # ----------------------------------------------------------------- kill

    def kill(self, runtime_id: str, project_id: str) -> Runtime:
        with self._op_lock(runtime_id):
            runtime = self._get_owned(runtime_id, project_id)
            if runtime.state in TERMINAL_RUNTIME_STATES:
                return runtime   # idempotent

            if runtime.sandbox_id:
                self._provider.kill(runtime.sandbox_id)

            runtime = self._save(transition(runtime, RuntimeState.KILLED))
            self._release_slot(runtime.workspace_id, runtime.id)
            self._emit(EventType.RUNTIME_KILLED, runtime.id)
            return runtime

    # -------------------------------------------------------------- destroy

    def destroy(self, runtime_id: str, project_id: str | None = None) -> None:
        with self._op_lock(runtime_id):
            with self._lock:
                runtime = self._runtimes.get(runtime_id)
            if runtime is None:
                return   # idempotent: destroying an unknown id is a no-op

            if project_id is not None and runtime.project_id != project_id:
                raise RedstoneRuntimeError(RuntimeErrorCode.NOT_OWNED)

            if runtime.state is RuntimeState.DESTROYED:
                return   # idempotent

            if runtime.sandbox_id:
                try:
                    self._provider.destroy(runtime.sandbox_id)
                except RedstoneSandboxError as exc:
                    raise RedstoneRuntimeError(RuntimeErrorCode.DESTROY_FAILED,
                                               safe_message=exc.safe_message) from exc

            runtime = self._save(transition(runtime, RuntimeState.DESTROYED))
            self._release_slot(runtime.workspace_id, runtime.id)
            self._emit(EventType.RUNTIME_DESTROYED, runtime.id)

    # ------------------------------------------------------------- restart

    def restart(self, runtime_id: str, project_id: str, workspace: Workspace) -> Runtime:
        """Destroy the current generation and start a fresh one for the same
        project/workspace. The runtime_id changes -- see docs/redstone/
        RUNTIME.md "one active runtime + historical generations" for why:
        it avoids ever needing to re-enter a terminal state, which would
        otherwise require a special-cased hole in the state machine.
        """
        old = self._get_owned(runtime_id, project_id)
        framework = old.framework

        self.destroy(runtime_id, project_id)
        # destroy() released the workspace slot; verify before proceeding so
        # a concurrent create() cannot have raced in ahead of us.
        with self._lock:
            still_held = self._workspace_active.get(workspace.id)
        if still_held not in (None, runtime_id):
            raise RedstoneRuntimeError(RuntimeErrorCode.BUSY)

        new_runtime = self.create(project_id, workspace, framework)
        try:
            return self.start(new_runtime.id, project_id, workspace)
        except RedstoneRuntimeError:
            # start() already marked new_runtime FAILED and released its
            # slot; nothing is left ambiguously "running".
            raise

    # -------------------------------------------------------- reconciliation

    def reconcile(self) -> int:
        """Detect and clean up runtimes whose sandbox no longer matches our
        recorded state -- a process that crashed between health checks, or
        (for providers that support it) a container left over from a prior
        Redstone process that never got a chance to call destroy(). Returns
        the number of runtimes corrected."""
        corrected = 0
        with self._lock:
            snapshot = list(self._runtimes.values())

        for runtime in snapshot:
            if runtime.state in TERMINAL_RUNTIME_STATES or runtime.sandbox_id is None:
                continue
            status = self._provider.status(runtime.sandbox_id)
            if status.state not in (SandboxState.RUNNING, SandboxState.STARTING):
                self._mark_crashed(runtime)
                corrected += 1

        return corrected
