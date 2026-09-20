"""Local-process sandbox provider.

**This provider enforces NO security isolation.** It runs the configured
command as a plain child process of the Redstone backend, sharing the host
filesystem, kernel, and (aside from the explicit environment dict) the same
privilege level as Redstone itself. It exists for two reasons, and neither of
them is "a security boundary":

  1. Development on a machine without Docker -- Phase 2A.1 already documented
     this exact tradeoff for the filesystem layer ("a local process runtime is
     not a security boundary"), and this class is that same tradeoff applied
     to sandbox execution.
  2. Fast, portable tests of RuntimeManager's *lifecycle logic* -- state
     transitions, ownership, concurrency, idempotency -- none of which need
     real isolation to verify, and all of which should run in CI without a
     Docker daemon available.

RuntimeManager and every caller above it must treat this provider as
explicitly untrusted-boundary. `is_isolated` is False specifically so a
caller can refuse to run it against a real, untrusted, AI-generated project
without an operator opting in.

Process-tree cleanup is best-effort: POSIX kills the process group; Windows
uses taskkill /T. Detached descendants can still escape either mechanism.
This provider is development-only and never a security boundary.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from io import BufferedReader
from typing import cast

from ..errors import RedstoneSandboxError, SandboxErrorCode
from ..models import SandboxConfig, SandboxResult, SandboxState, SandboxStatus

__all__ = ["LocalProcessSandboxProvider"]

_IS_POSIX = os.name == "posix"


@dataclass
class _Handle:
    process: subprocess.Popen
    config: SandboxConfig
    state: SandboxState = SandboxState.CREATED
    log_data: bytearray = field(default_factory=bytearray)
    log_bytes: int = 0
    log_lock: threading.Lock = field(default_factory=threading.Lock)
    reader: threading.Thread | None = None


class LocalProcessSandboxProvider:
    name = "local_process"
    is_isolated = False   # see module docstring; checked explicitly by callers

    def __init__(self) -> None:
        self._handles: dict[str, _Handle] = {}

    # ------------------------------------------------------------- create

    def create(self, config: SandboxConfig) -> str:
        if len(config.mounts) != 1:
            raise RedstoneSandboxError(
                SandboxErrorCode.UNSUPPORTED_OPERATION,
                safe_message="the local process provider supports exactly one mount",
            )
        mount = config.mounts[0]
        if not mount.host_path.is_dir():
            raise RedstoneSandboxError(SandboxErrorCode.CREATE_FAILED,
                                       internal="mount source is not a directory")

        sandbox_id = f"local-{uuid.uuid4().hex[:16]}"
        self._pending = getattr(self, "_pending", {})
        self._pending[sandbox_id] = (config, mount.host_path)
        return sandbox_id

    # -------------------------------------------------------------- start

    def start(self, sandbox_id: str) -> None:
        config, workdir = self._pending.pop(sandbox_id, (None, None))
        if config is None:
            raise RedstoneSandboxError(SandboxErrorCode.NOT_FOUND)

        argv = list(config.command.resolve())
        # Resolve the interpreter/binary on PATH explicitly; never build a
        # command from string concatenation. shutil.which mirrors what the
        # OS would find, so a missing tool fails clearly rather than
        # silently picking up something unexpected from an inherited PATH.
        resolved = shutil.which(argv[0])
        if resolved:
            argv[0] = resolved

        try:
            process = subprocess.Popen(
                argv,
                cwd=str(workdir),
                env=dict(config.environment),   # explicit only; no os.environ merge
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if not _IS_POSIX else 0,
                start_new_session=_IS_POSIX,   # POSIX: own process group, for tree-kill
            )
        except OSError as exc:
            raise RedstoneSandboxError(SandboxErrorCode.START_FAILED, internal=str(type(exc)))

        handle = _Handle(process=process, config=config, state=SandboxState.RUNNING)
        self._handles[sandbox_id] = handle
        handle.reader = threading.Thread(target=self._drain_output, args=(handle,), daemon=True)
        handle.reader.start()

    @staticmethod
    def _drain_output(handle: _Handle) -> None:
        assert handle.process.stdout is not None
        limit = handle.config.resource_limits.output_bytes
        while chunk := cast(BufferedReader, handle.process.stdout).read1(4096):
            with handle.log_lock:
                handle.log_bytes += len(chunk)
                remaining = limit - len(handle.log_data)
                if remaining > 0:
                    handle.log_data.extend(chunk[:remaining])

    # --------------------------------------------------------------- wait

    def wait(self, sandbox_id: str, timeout: float) -> SandboxResult:
        handle = self._require(sandbox_id)
        started = time.monotonic()
        try:
            handle.process.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            self.kill(sandbox_id)

        if handle.reader is not None:
            handle.reader.join(timeout=1)
        with handle.log_lock:
            output = bytes(handle.log_data).decode("utf-8", "ignore")
            truncated = handle.log_bytes > len(handle.log_data)

        handle.state = SandboxState.KILLED if timed_out else (
            SandboxState.STOPPED if handle.process.returncode == 0 else SandboxState.FAILED
        )
        return SandboxResult(
            exit_code=handle.process.returncode if not timed_out else None,
            stdout=output, stderr="",
            truncated=truncated,
            timed_out=timed_out, duration_seconds=time.monotonic() - started,
        )

    # ------------------------------------------------------------- exec_in

    def exec_in(self, sandbox_id: str, argv: tuple[str, ...], timeout: float) -> SandboxResult:
        # There is no real sandbox boundary to "enter"; a best-effort emulation
        # runs the command in the same working directory. Not a health-check
        # primitive to trust for anything security-relevant.
        handle = self._require(sandbox_id)
        mount = handle.config.mounts[0]
        started = time.monotonic()
        command = list(argv)
        resolved = shutil.which(command[0])
        if resolved:
            command[0] = resolved
        try:
            result = subprocess.run(
                command, cwd=str(mount.host_path), env=dict(handle.config.environment),
                capture_output=True, text=True,
                timeout=timeout,
            )
            return SandboxResult(
                exit_code=result.returncode, stdout=result.stdout[-8192:],
                stderr=result.stderr[-8192:], truncated=False,
                timed_out=False, duration_seconds=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(exit_code=None, stdout="", stderr="", truncated=False,
                                 timed_out=True, duration_seconds=time.monotonic() - started)
        except OSError:
            raise RedstoneSandboxError(SandboxErrorCode.EXEC_FAILED)

    # ------------------------------------------------------------- status

    def status(self, sandbox_id: str) -> SandboxStatus:
        handle = self._handles.get(sandbox_id)
        if handle is None:
            return SandboxStatus(sandbox_id=sandbox_id, state=SandboxState.DESTROYED)
        if handle.process.poll() is None:
            state = SandboxState.RUNNING
        else:
            state = handle.state if handle.state in (SandboxState.KILLED,) else (
                SandboxState.STOPPED if handle.process.returncode == 0 else SandboxState.FAILED
            )
        return SandboxStatus(
            sandbox_id=sandbox_id, state=state,
            exit_code=handle.process.poll(),
            enforced_limits=(),   # nothing here is actually enforced; see module docstring
        )

    # --------------------------------------------------------------- logs

    def logs(self, sandbox_id: str, max_bytes: int) -> str:
        handle = self._require(sandbox_id)
        with handle.log_lock:
            data = bytes(handle.log_data)
        return data[-max_bytes:].decode("utf-8", "ignore")

    # --------------------------------------------------------------- stop

    def stop(self, sandbox_id: str, timeout: float) -> None:
        handle = self._handles.get(sandbox_id)
        if handle is None or handle.process.poll() is not None:
            return
        self._signal(handle, signal.SIGTERM if _IS_POSIX else signal.CTRL_BREAK_EVENT)
        try:
            handle.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill(sandbox_id)

    # --------------------------------------------------------------- kill

    def kill(self, sandbox_id: str) -> None:
        handle = self._handles.get(sandbox_id)
        if handle is None or handle.process.poll() is not None:
            return
        # sys.platform rather than _IS_POSIX: equivalent at runtime, and the
        # form a type checker can narrow the POSIX-only os/signal APIs on.
        if sys.platform != "win32":
            try:
                os.killpg(os.getpgid(handle.process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(handle.process.pid), "/T", "/F"],
                    capture_output=True, timeout=5,
                )
                if result.returncode != 0 and handle.process.poll() is None:
                    handle.process.kill()
            except (OSError, subprocess.TimeoutExpired):
                handle.process.kill()
        try:
            handle.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        handle.state = SandboxState.KILLED

    # ------------------------------------------------------------ destroy

    def destroy(self, sandbox_id: str) -> None:
        getattr(self, "_pending", {}).pop(sandbox_id, None)
        handle = self._handles.get(sandbox_id)
        if handle is not None and handle.process.poll() is None:
            self.kill(sandbox_id)
        self._handles.pop(sandbox_id, None)
        if handle is not None:
            handle.state = SandboxState.DESTROYED

    def preview_upstream(self, sandbox_id: str) -> None:
        """Always None: this provider isolates nothing, so Redstone refuses
        to serve an untrusted preview through it."""
        return None

    # ------------------------------------------------------ list_managed

    def list_managed(self) -> tuple[dict, ...]:
        """No durable process registry; orphaned children may survive restart.
        Local execution is development-only and cannot provide cleanup proof."""
        return ()

    # ------------------------------------------------------------- helpers

    def _require(self, sandbox_id: str) -> _Handle:
        handle = self._handles.get(sandbox_id)
        if handle is None:
            raise RedstoneSandboxError(SandboxErrorCode.NOT_FOUND)
        return handle

    @staticmethod
    def _signal(handle: _Handle, sig: int) -> None:
        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(handle.process.pid), sig)
            else:
                handle.process.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass
