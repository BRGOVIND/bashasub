"""The SandboxProvider contract.

RuntimeManager (redstone.runtime) depends on this Protocol and nothing else --
no `import docker`, no subprocess call of its own. Swapping Docker for
containerd, Podman, or a microVM later means writing one new class here and
registering it; RuntimeManager does not change.

Two execution shapes are covered:
  * a BOUNDED operation (install/typecheck/lint/build): create -> start ->
    wait (blocks until exit or timeout, killing the tree on timeout);
  * a LONG-RUNNING operation (the dev server): create -> start -> (caller
    polls status()/exec_in() for health) -> stop/kill -> destroy.
"""

from __future__ import annotations

from typing import Protocol

from .models import SandboxConfig, SandboxResult, SandboxStatus

__all__ = ["SandboxProvider"]


class SandboxProvider(Protocol):
    name: str

    def create(self, config: SandboxConfig) -> str:
        """Prepare (but do not start) one sandboxed execution. Returns a
        provider-internal sandbox_id; never a raw host container ID exposed
        to a public API."""
        ...

    def start(self, sandbox_id: str) -> None:
        """Begin running the configured command."""
        ...

    def wait(self, sandbox_id: str, timeout: float) -> SandboxResult:
        """Block for a bounded operation to finish. On timeout, the ENTIRE
        process tree is terminated (not just the top-level process) and the
        result has timed_out=True -- this is the guarantee Phase 4 exists to
        provide, not an incidental detail."""
        ...

    def exec_in(self, sandbox_id: str, argv: tuple[str, ...], timeout: float) -> SandboxResult:
        """Run one additional bounded command inside an already-running
        sandbox, e.g. a health-check probe. Must work even when the
        sandbox's own NetworkPolicy is DENY, since it operates within the
        sandbox's existing namespace rather than over its network."""
        ...

    def status(self, sandbox_id: str) -> SandboxStatus:
        ...

    def logs(self, sandbox_id: str, max_bytes: int) -> str:
        """Bounded, already-truncated output. Never returns more than
        max_bytes regardless of how much the process actually produced."""
        ...

    def stop(self, sandbox_id: str, timeout: float) -> None:
        """Ask the sandbox to exit gracefully, falling back to kill() if it
        has not exited within `timeout`. Idempotent: stopping an
        already-stopped sandbox must not raise."""
        ...

    def kill(self, sandbox_id: str) -> None:
        """Terminate immediately, including descendants. Idempotent."""
        ...

    def destroy(self, sandbox_id: str) -> None:
        """Remove every resource associated with the sandbox. Idempotent --
        destroying an already-destroyed or never-created id must not raise."""
        ...
