"""A fake SandboxProvider for RuntimeManager unit tests.

Per the Phase 4/5 testing policy, mocking is reserved for this one layer:
RuntimeManager's own lifecycle/ownership/concurrency logic is verified against
this in-memory fake, while sandbox isolation itself (Docker) is verified
separately against the real daemon in test_sandbox.py and
test_runtime_docker_integration.py. Nothing here re-implements or asserts
anything about real isolation.
"""

from __future__ import annotations

import threading
import time

from redstone.sandbox.errors import RedstoneSandboxError, SandboxErrorCode
from redstone.sandbox.models import SandboxResult, SandboxState, SandboxStatus

__all__ = ["FakeSandboxProvider"]


class FakeSandboxProvider:
    """Configurable in-memory stand-in for a real SandboxProvider.

    - `install_ok`: whether a bounded wait() (install) reports success.
    - `health_check_failures`: how many exec_in() health probes fail before
      one succeeds (0 = healthy on the first probe).
    - `never_healthy`: every exec_in() probe fails; startup should time out.
    - `start_delay` / `create_delay`: sleep before returning, to open a
      deterministic window for concurrency tests.
    """

    name = "fake"
    is_isolated = False

    def __init__(
        self,
        *,
        install_ok: bool = True,
        health_check_failures: int = 0,
        never_healthy: bool = False,
        start_delay: float = 0.0,
        create_delay: float = 0.0,
        wait_result: SandboxResult | None = None,
        create_error: RedstoneSandboxError | None = None,
    ) -> None:
        self.install_ok = install_ok
        self.health_check_failures = health_check_failures
        self.never_healthy = never_healthy
        self.start_delay = start_delay
        self.create_delay = create_delay
        self.wait_result = wait_result
        self.create_error = create_error

        self._lock = threading.Lock()
        self._sandboxes: dict[str, dict] = {}
        self._counter = 0

        self.create_calls: list[str] = []
        self.start_calls: list[str] = []
        self.wait_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.kill_calls: list[str] = []
        self.destroy_calls: list[str] = []

    # ---------------------------------------------------------------- helpers

    def force_state(self, sandbox_id: str, state: SandboxState) -> None:
        with self._lock:
            if sandbox_id in self._sandboxes:
                self._sandboxes[sandbox_id]["state"] = state

    def live_sandbox_ids(self) -> set[str]:
        with self._lock:
            return set(self._sandboxes.keys())

    # ------------------------------------------------------------ the contract

    def create(self, config) -> str:
        if self.create_delay:
            time.sleep(self.create_delay)
        if self.create_error is not None:
            raise self.create_error
        with self._lock:
            self._counter += 1
            sandbox_id = f"sbx_{self._counter}"
            self._sandboxes[sandbox_id] = {
                "state": SandboxState.CREATED, "config": config, "health_calls": 0,
            }
        self.create_calls.append(sandbox_id)
        return sandbox_id

    def start(self, sandbox_id: str) -> None:
        if self.start_delay:
            time.sleep(self.start_delay)
        self.start_calls.append(sandbox_id)
        with self._lock:
            info = self._sandboxes.get(sandbox_id)
            if info is None:
                raise RedstoneSandboxError(SandboxErrorCode.NOT_FOUND)
            info["state"] = SandboxState.RUNNING

    def wait(self, sandbox_id: str, timeout: float) -> SandboxResult:
        self.wait_calls.append(sandbox_id)
        with self._lock:
            info = self._sandboxes.get(sandbox_id)
            if info is None:
                raise RedstoneSandboxError(SandboxErrorCode.NOT_FOUND)
            info["state"] = SandboxState.STOPPED if self.install_ok else SandboxState.FAILED

        if self.wait_result is not None:
            return self.wait_result

        if self.install_ok:
            return SandboxResult(exit_code=0, stdout="installed", stderr="",
                                truncated=False, timed_out=False, duration_seconds=0.01)
        return SandboxResult(exit_code=1, stdout="", stderr="npm error: failed",
                            truncated=False, timed_out=False, duration_seconds=0.01)

    def exec_in(self, sandbox_id: str, argv: tuple, timeout: float) -> SandboxResult:
        with self._lock:
            info = self._sandboxes.get(sandbox_id)
            if info is None:
                raise RedstoneSandboxError(SandboxErrorCode.NOT_FOUND)
            info["health_calls"] += 1
            calls = info["health_calls"]

        if self.never_healthy or calls <= self.health_check_failures:
            return SandboxResult(exit_code=1, stdout="", stderr="",
                                truncated=False, timed_out=False, duration_seconds=0.01)
        return SandboxResult(exit_code=0, stdout="ok", stderr="",
                            truncated=False, timed_out=False, duration_seconds=0.01)

    def status(self, sandbox_id: str) -> SandboxStatus:
        with self._lock:
            info = self._sandboxes.get(sandbox_id)
        if info is None:
            return SandboxStatus(sandbox_id=sandbox_id, state=SandboxState.DESTROYED)
        return SandboxStatus(sandbox_id=sandbox_id, state=info["state"])

    def logs(self, sandbox_id: str, max_bytes: int) -> str:
        return "fake sandbox log output"[:max_bytes]

    def stop(self, sandbox_id: str, timeout: float) -> None:
        self.stop_calls.append(sandbox_id)
        with self._lock:
            if sandbox_id in self._sandboxes:
                self._sandboxes[sandbox_id]["state"] = SandboxState.STOPPED

    def kill(self, sandbox_id: str) -> None:
        self.kill_calls.append(sandbox_id)
        with self._lock:
            if sandbox_id in self._sandboxes:
                self._sandboxes[sandbox_id]["state"] = SandboxState.KILLED

    def destroy(self, sandbox_id: str) -> None:
        self.destroy_calls.append(sandbox_id)
        with self._lock:
            self._sandboxes.pop(sandbox_id, None)
