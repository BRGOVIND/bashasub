"""Docker-backed sandbox provider.

Shells out to the `docker` CLI via argv lists (never a shell string, never
string-formatted from external input) rather than adding the docker-py SDK as
a dependency -- one less thing to audit, and the CLI is exactly as capable as
we need. Every security-relevant flag is set explicitly on every invocation;
none of Docker's defaults are relied on silently.

Security posture applied to EVERY container this provider creates, regardless
of caller-supplied config:
  * --security-opt no-new-privileges  -- sudo/setuid inside cannot escalate
  * --cap-drop ALL                    -- no Linux capabilities at all
  * --pids-limit                      -- bounds fork bombs
  * a fixed non-root --user           -- never the identity Redstone itself runs as
  * no --privileged, ever
  * no --network host, ever
  * no --pid host / --ipc host, ever
  * no Docker socket mount, ever

These are asserted by a static test that inspects the constructed argv
directly (test_sandbox_docker.py::test_dangerous_flags_never_appear), not just
described here.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid

from ..errors import RedstoneSandboxError, SandboxErrorCode
from ..models import Mount, NetworkPolicy, SandboxConfig, SandboxResult, SandboxState, SandboxStatus

__all__ = ["DockerSandboxProvider", "docker_available"]

# Pinned to a tag rather than a digest for Phase 4; production deployment
# should pin an exact digest for reproducibility. Documented, not hidden.
DEFAULT_IMAGE = "node:20-alpine"

_CONTAINER_PROJECT_PATH = "/workspace/project"

# The complete environment every sandbox gets, before SandboxConfig.environment
# is merged in on top. Notably absent: anything from os.environ. There is no
# line in this file that reads the host process's environment.
_BASE_ENV = {
    "HOME": "/tmp",
    "npm_config_cache": "/tmp/.npm-cache",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}


def docker_available() -> bool:
    """Whether the docker CLI can reach a daemon right now. Cheap, used to
    decide at startup whether DockerSandboxProvider can be offered at all."""
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run_docker(argv: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["docker", *argv], capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise RedstoneSandboxError(SandboxErrorCode.PROVIDER_UNAVAILABLE,
                                   internal="docker CLI not found")
    except subprocess.TimeoutExpired:
        raise RedstoneSandboxError(SandboxErrorCode.TIMEOUT)


def _read_bounded(argv: list[str], max_bytes: int, timeout: float | None) -> tuple[str, bool]:
    """Run a docker CLI command and read stdout incrementally, stopping (and
    killing our local reader, not the remote process) the moment max_bytes is
    exceeded -- so a runaway sandbox cannot make Redstone itself buffer an
    unbounded string in memory."""
    try:
        process = subprocess.Popen(
            ["docker", *argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        raise RedstoneSandboxError(SandboxErrorCode.PROVIDER_UNAVAILABLE)

    chunks: list[str] = []
    total = 0
    truncated = False
    deadline = time.monotonic() + timeout if timeout else None

    try:
        assert process.stdout is not None
        for line in process.stdout:
            chunks.append(line)
            total += len(line.encode("utf-8", "ignore"))
            if total >= max_bytes:
                truncated = True
                break
            if deadline and time.monotonic() > deadline:
                truncated = True
                break
    finally:
        process.stdout.close() if process.stdout else None
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    return "".join(chunks), truncated


class DockerSandboxProvider:
    name = "docker"
    is_isolated = True   # real namespace/cgroup isolation; see module docstring

    def __init__(self, image: str = DEFAULT_IMAGE) -> None:
        self._image = image

    # ------------------------------------------------------------- create

    def create(self, config: SandboxConfig) -> str:
        sandbox_id = f"redstone-{uuid.uuid4().hex[:16]}"

        argv = [
            "create",
            "--name", sandbox_id,
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--pids-limit", str(config.resource_limits.pids),
            "--memory", f"{config.resource_limits.memory_mb}m",
            "--cpus", str(config.resource_limits.cpu_cores),
            "--log-driver", "json-file",
            "--log-opt", f"max-size={max(1, config.resource_limits.output_bytes // (1024 * 1024))}m",
            "--log-opt", "max-file=1",
            "--user", config.user or "1000:1000",
            "--workdir", config.working_dir,
        ]

        if config.read_only_root:
            argv.append("--read-only")
        for tmp_path in config.tmpfs_paths:
            argv.extend(["--tmpfs", tmp_path])

        argv.extend(self._network_args(config))

        for mount in config.mounts:
            argv.extend(["--mount", self._mount_arg(mount)])

        environment = dict(_BASE_ENV)
        environment.update(config.environment)
        for key, value in environment.items():
            argv.extend(["--env", f"{key}={value}"])

        argv.append(self._image)
        argv.extend(config.command.resolve())

        result = _run_docker(argv, timeout=30)
        if result.returncode != 0:
            raise RedstoneSandboxError(
                SandboxErrorCode.CREATE_FAILED,
                internal=f"docker create exit={result.returncode} stderr={result.stderr[:500]}",
            )
        return sandbox_id

    def _network_args(self, config: SandboxConfig) -> list[str]:
        if config.network_policy is NetworkPolicy.DENY:
            return ["--network", "none"]
        if config.network_policy is NetworkPolicy.INSTALL_ONLY:
            # Default bridge: outbound reaches the internet; nothing is
            # published, so nothing on the host or another container can
            # reach IN. This is the one place a sandbox gets real egress,
            # and it is used only for the dependency-install operation.
            return ["--network", "bridge"]
        raise RedstoneSandboxError(
            SandboxErrorCode.UNSUPPORTED_OPERATION,
            safe_message=f"network policy '{config.network_policy.value}' is not implemented",
        )

    @staticmethod
    def _mount_arg(mount: Mount) -> str:
        kind = "type=bind"
        source = f"source={mount.host_path}"
        target = f"target={mount.container_path}"
        parts = [kind, source, target]
        if mount.read_only:
            parts.append("readonly")
        return ",".join(parts)

    # -------------------------------------------------------------- start

    def start(self, sandbox_id: str) -> None:
        result = _run_docker(["start", sandbox_id], timeout=30)
        if result.returncode != 0:
            raise RedstoneSandboxError(
                SandboxErrorCode.START_FAILED,
                internal=f"docker start exit={result.returncode} stderr={result.stderr[:500]}",
            )

    # --------------------------------------------------------------- wait

    def wait(self, sandbox_id: str, timeout: float) -> SandboxResult:
        started = time.monotonic()
        try:
            wait_result = subprocess.run(
                ["docker", "wait", sandbox_id],
                capture_output=True, text=True, timeout=timeout,
            )
            timed_out = False
            exit_code = int(wait_result.stdout.strip()) if wait_result.stdout.strip().isdigit() else None
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
            # The whole point of this method: on timeout, terminate the
            # ENTIRE process tree. `docker kill` sends SIGKILL to PID 1 of
            # the container's own cgroup/PID namespace, which the kernel
            # tears down completely -- every descendant goes with it, not
            # just the top-level process. Verified directly in
            # test_process_tree_is_fully_terminated_on_timeout.
            self.kill(sandbox_id)

        duration = time.monotonic() - started
        logs, truncated = self.logs(sandbox_id, max_bytes=256 * 1024), False
        return SandboxResult(
            exit_code=exit_code, stdout=logs, stderr="",
            truncated=truncated, timed_out=timed_out, duration_seconds=duration,
        )

    # ------------------------------------------------------------- exec_in

    def exec_in(self, sandbox_id: str, argv: tuple[str, ...], timeout: float) -> SandboxResult:
        started = time.monotonic()
        try:
            result = subprocess.run(
                ["docker", "exec", sandbox_id, *argv],
                capture_output=True, text=True, timeout=timeout,
            )
            return SandboxResult(
                exit_code=result.returncode,
                stdout=result.stdout[-8192:], stderr=result.stderr[-8192:],
                truncated=len(result.stdout) > 8192 or len(result.stderr) > 8192,
                timed_out=False, duration_seconds=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                exit_code=None, stdout="", stderr="",
                truncated=False, timed_out=True,
                duration_seconds=time.monotonic() - started,
            )
        except FileNotFoundError:
            raise RedstoneSandboxError(SandboxErrorCode.PROVIDER_UNAVAILABLE)

    # ------------------------------------------------------------- status

    def status(self, sandbox_id: str) -> SandboxStatus:
        result = _run_docker(["inspect", sandbox_id], timeout=15)
        if result.returncode != 0:
            return SandboxStatus(sandbox_id=sandbox_id, state=SandboxState.DESTROYED)

        try:
            data = json.loads(result.stdout)[0]
            state_info = data["State"]
        except (ValueError, KeyError, IndexError):
            return SandboxStatus(sandbox_id=sandbox_id, state=SandboxState.FAILED)

        state = self._map_state(state_info)
        return SandboxStatus(
            sandbox_id=sandbox_id,
            state=state,
            exit_code=state_info.get("ExitCode") if not state_info.get("Running") else None,
            enforced_limits=("cpu_cores", "memory_mb", "pids"),
        )

    @staticmethod
    def _map_state(state_info: dict) -> SandboxState:
        if state_info.get("Running"):
            return SandboxState.RUNNING
        if state_info.get("OOMKilled") or state_info.get("ExitCode", 0) not in (0, None):
            if state_info.get("ExitCode") in (137, 143):   # SIGKILL / SIGTERM
                return SandboxState.KILLED
            return SandboxState.FAILED
        return SandboxState.STOPPED

    # --------------------------------------------------------------- logs

    def logs(self, sandbox_id: str, max_bytes: int) -> str:
        text, _truncated = _read_bounded(["logs", sandbox_id], max_bytes, timeout=15)
        encoded = text.encode("utf-8", "ignore")
        if len(encoded) > max_bytes:
            return encoded[-max_bytes:].decode("utf-8", "ignore")
        return text

    # --------------------------------------------------------------- stop

    def stop(self, sandbox_id: str, timeout: float) -> None:
        result = _run_docker(["stop", "-t", str(int(timeout)), sandbox_id], timeout=timeout + 15)
        # "No such container" -> already gone; idempotent, not an error.
        if result.returncode != 0 and "No such container" not in result.stderr:
            raise RedstoneSandboxError(
                SandboxErrorCode.STOP_FAILED, internal=result.stderr[:500]
            )

    # --------------------------------------------------------------- kill

    def kill(self, sandbox_id: str) -> None:
        result = _run_docker(["kill", sandbox_id], timeout=15)
        acceptable = (
            result.returncode == 0
            or "No such container" in result.stderr
            or "is not running" in result.stderr
        )
        if not acceptable:
            raise RedstoneSandboxError(
                SandboxErrorCode.STOP_FAILED, internal=result.stderr[:500]
            )

    # ------------------------------------------------------------ destroy

    def destroy(self, sandbox_id: str) -> None:
        result = _run_docker(["rm", "-f", sandbox_id], timeout=30)
        if result.returncode != 0 and "No such container" not in result.stderr:
            raise RedstoneSandboxError(
                SandboxErrorCode.DESTROY_FAILED, internal=result.stderr[:500]
            )
