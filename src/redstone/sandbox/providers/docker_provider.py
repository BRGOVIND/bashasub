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
  * a redstone.managed=true label     -- ownership marker for orphan recovery
  * no --privileged, ever
  * no --network host, ever
  * no --pid host / --ipc host, ever
  * no --device, ever
  * no port publishing (-p/--publish), ever
  * no Docker socket mount, ever

These are asserted by a static test that inspects the constructed argv
directly (test_dangerous_flags_never_appear_in_the_constructed_argv), not just
described here.
"""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from ..errors import RedstoneSandboxError, SandboxErrorCode
from ..models import Mount, NetworkPolicy, SandboxConfig, SandboxResult, SandboxState, SandboxStatus

__all__ = ["DockerSandboxProvider", "docker_available", "MANAGED_LABEL", "DEFAULT_IMAGE"]

# Digest-pinned (Phase 4.1). The digest is the one `node:20-alpine` resolved
# to on this daemon at hardening time; a tag can be repointed upstream, a
# digest cannot. Rotating it is a deliberate, reviewed change to this line --
# never something a project, an API caller or the agent can select.
DEFAULT_IMAGE = (
    "node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293"
)

_CONTAINER_PROJECT_PATH = "/workspace/project"

# Every container this provider creates carries this label, unconditionally.
# Orphan reconciliation filters on it at the Docker CLI level, so a container
# without it can never even appear in list_managed() -- let alone be touched.
MANAGED_LABEL = "redstone.managed"
_LABEL_PREFIX = "redstone."

# The complete environment every sandbox gets, before SandboxConfig.environment
# is merged in on top. Notably absent: anything from os.environ. There is no
# line in this file that reads the host process's environment.
_BASE_ENV = {
    "HOME": "/tmp",
    "npm_config_cache": "/tmp/.npm-cache",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}

# Upper bound on how long reading a finished container's logs may take.
_LOG_READ_TIMEOUT_SECONDS = 15.0

# Install egress proxy (Phase 4.2). Redstone-authored, mounted read-only into
# a container running the same digest-pinned image -- no third-party proxy
# image, no new supply chain.
PROXY_SCRIPT = Path(__file__).resolve().parent.parent / "egress_proxy.js"
_PROXY_CONTAINER_PATH = "/opt/redstone/egress_proxy.js"
_PROXY_PORT = 3128
_PROXY_READY = "REDSTONE_EGRESS_PROXY_READY"
_PROXY_ROLE_LABEL = "redstone.role"


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


def _trim_tail(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8", "ignore")
    if len(encoded) <= max_bytes:
        return text
    return encoded[-max_bytes:].decode("utf-8", "ignore")


def _read_bounded(argv: list[str], max_bytes: int, timeout: float | None) -> tuple[str, bool]:
    """Run a docker CLI command and read its merged stdout+stderr
    incrementally, stopping (and killing our local reader, not the remote
    process) the moment max_bytes is exceeded -- so a runaway sandbox cannot
    make Redstone itself buffer an unbounded string in memory."""
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
            if total > max_bytes:
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

    return _trim_tail("".join(chunks), max_bytes), truncated


class _BoundedSink:
    """Accumulates one stream up to max_bytes, keeps counting (so truncation
    is reported exactly) but stops retaining text past the ceiling."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.kept: list[str] = []
        self.kept_bytes = 0
        self.seen_bytes = 0

    def feed(self, line: str) -> None:
        size = len(line.encode("utf-8", "ignore"))
        self.seen_bytes += size
        if self.kept_bytes >= self.max_bytes:
            return
        self.kept.append(line)
        self.kept_bytes += size

    @property
    def truncated(self) -> bool:
        return self.seen_bytes > self.max_bytes

    def text(self) -> str:
        # Keep the head: for install/build diagnostics the first error is the
        # useful one, and a flood is usually a repeat of it.
        encoded = "".join(self.kept).encode("utf-8", "ignore")[: self.max_bytes]
        return encoded.decode("utf-8", "ignore")


def _read_bounded_split(
    argv: list[str], max_bytes: int, timeout: float,
) -> tuple[str, bool, str, bool, int | None, bool]:
    """Run a docker CLI command with GENUINELY separate stdout/stderr pipes.

    `docker logs` reproduces each container stream on the matching CLI
    stream (verified against this daemon), so reading two pipes yields the
    container's real stdout and stderr, not an interleaved blob. Both pipes
    are drained concurrently to completion -- a reader that stopped draining
    one pipe could deadlock the CLI on a full pipe buffer -- while each
    sink independently stops RETAINING text past max_bytes. Memory held by
    Redstone is therefore bounded by 2 * max_bytes regardless of how much the
    sandbox wrote.
    """
    try:
        process = subprocess.Popen(
            ["docker", *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            errors="replace",
        )
    except FileNotFoundError:
        raise RedstoneSandboxError(SandboxErrorCode.PROVIDER_UNAVAILABLE)

    out_sink = _BoundedSink(max_bytes)
    err_sink = _BoundedSink(max_bytes)

    def _drain(stream, sink: _BoundedSink) -> None:
        try:
            for line in stream:
                sink.feed(line)
        except (ValueError, OSError):
            pass

    readers = [
        threading.Thread(target=_drain, args=(process.stdout, out_sink), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, err_sink), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait(timeout=5)
    finally:
        for reader in readers:
            reader.join(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()

    exit_code = None if timed_out else process.returncode
    return (out_sink.text(), out_sink.truncated, err_sink.text(), err_sink.truncated,
            exit_code, timed_out)


def _directory_size(root: Path) -> int:
    """Bytes actually on disk under `root`, never following links (a link
    planted by the sandbox must not make us measure -- or walk into --
    anything outside the mount)."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


class DockerSandboxProvider:
    name = "docker"
    is_isolated = True   # real namespace/cgroup isolation; see module docstring

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        *,
        storage_poll_interval: float = 1.0,
        egress_proxy_script: Path | None = None,
    ) -> None:
        self._image = image
        self._storage_poll_interval = storage_poll_interval
        self._proxy_script = Path(egress_proxy_script) if egress_proxy_script else PROXY_SCRIPT
        # Docker remembers the container; it does not remember OUR config.
        # These are the few per-sandbox facts later calls need (output
        # ceiling, storage ceiling + what to measure, watchdog handle).
        self._lock = threading.Lock()
        self._sandboxes: dict[str, dict] = {}

    # ------------------------------------------------------------- create

    def create(self, config: SandboxConfig) -> str:
        sandbox_id = f"redstone-{uuid.uuid4().hex[:16]}"
        limits = config.resource_limits

        argv = [
            "create",
            "--name", sandbox_id,
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--pids-limit", str(limits.pids),
            "--memory", f"{limits.memory_mb}m",
            "--memory-swap", f"{limits.memory_mb}m",   # no swap headroom past --memory
            "--cpus", str(limits.cpu_cores),
            "--log-driver", "json-file",
            "--log-opt", f"max-size={max(1, limits.output_bytes // (1024 * 1024))}m",
            "--log-opt", "max-file=1",
            "--user", config.user or "1000:1000",
            "--workdir", config.working_dir,
            "--label", f"{MANAGED_LABEL}=true",
        ]

        for key, value in config.labels.items():
            argv.extend(["--label", self._label_arg(key, value)])

        if config.read_only_root:
            argv.append("--read-only")
        for tmp_path in config.tmpfs_paths:
            # Bounded tmpfs: without size= Docker would allow up to half of
            # host RAM, which is a second, uncounted storage channel.
            argv.extend(["--tmpfs", f"{tmp_path}:rw,size={limits.storage_mb}m,mode=1777"])

        for mount in config.mounts:
            argv.extend(["--mount", self._mount_arg(mount)])

        # Resolve the command before creating any Docker resource, so an
        # unsupported operation cannot leave a network or proxy behind.
        command_argv = list(config.command.resolve())
        try:
            network_args, network_env = self._network_args(config, sandbox_id)
        except RedstoneSandboxError:
            self._remove_network(sandbox_id)
            raise
        argv.extend(network_args)

        # Redstone's proxy settings are applied last so no caller value can
        # replace them. Enforcement does not depend on them: they only make
        # legitimate npm traffic find the proxy (see _start_egress_proxy).
        environment = dict(_BASE_ENV)
        environment.update(config.environment)
        environment.update(network_env)
        for key, value in environment.items():
            argv.extend(["--env", f"{key}={value}"])

        argv.append(self._image)
        argv.extend(command_argv)

        try:
            result = _run_docker(argv, timeout=30)
        except RedstoneSandboxError:
            self._remove_network(sandbox_id)
            raise
        if result.returncode != 0:
            self._remove_network(sandbox_id)
            raise RedstoneSandboxError(
                SandboxErrorCode.CREATE_FAILED,
                internal=f"docker create exit={result.returncode} stderr={result.stderr[:500]}",
            )

        with self._lock:
            self._sandboxes[sandbox_id] = {
                "output_bytes": limits.output_bytes,
                "storage_mb": limits.storage_mb,
                "watch_paths": tuple(m.host_path for m in config.mounts if not m.read_only),
                "stop_event": None,
                "limit_exceeded": None,
            }
        return sandbox_id

    @staticmethod
    def _label_arg(key: str, value: str) -> str:
        # Labels are ownership metadata, not a free-form channel: only
        # Redstone-namespaced keys, only printable single-line values.
        if not key.startswith(_LABEL_PREFIX) or key == MANAGED_LABEL:
            raise RedstoneSandboxError(
                SandboxErrorCode.INVALID_CONFIG, safe_message="Invalid sandbox label."
            )
        if not value.isprintable() or len(value) > 128:
            raise RedstoneSandboxError(
                SandboxErrorCode.INVALID_CONFIG, safe_message="Invalid sandbox label value."
            )
        return f"{key}={value}"

    # Names are derived from the sandbox id, never stored: after a Redstone
    # restart destroy() can still find and remove an orphan's proxy and
    # networks.
    @staticmethod
    def _network_name(sandbox_id: str) -> str:
        return f"{sandbox_id}-net"

    @staticmethod
    def _egress_network_name(sandbox_id: str) -> str:
        return f"{sandbox_id}-egress"

    @staticmethod
    def _proxy_name(sandbox_id: str) -> str:
        return f"{sandbox_id}-proxy"

    def _network_args(self, config: SandboxConfig, sandbox_id: str) -> tuple[list[str], dict[str, str]]:
        if config.network_policy is NetworkPolicy.DENY:
            return ["--network", "none"], {}
        if config.network_policy is NetworkPolicy.INSTALL_ONLY:
            proxy_url = f"http://{self._start_egress_proxy(config, sandbox_id)}:{_PROXY_PORT}"
            env = {name: proxy_url for name in (
                "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "npm_config_proxy", "npm_config_https_proxy",
            )}
            env.update({"NO_PROXY": "", "no_proxy": "",
                        "npm_config_registry": config.egress.registry_url,
                        "npm_config_update_notifier": "false"})
            return ["--network", self._network_name(sandbox_id)], env
        raise RedstoneSandboxError(
            SandboxErrorCode.UNSUPPORTED_OPERATION,
            safe_message=f"network policy '{config.network_policy.value}' is not implemented",
        )

    def _checked(self, argv: list[str], message: str, timeout: float = 30) -> subprocess.CompletedProcess:
        result = _run_docker(argv, timeout=timeout)
        if result.returncode != 0:
            raise RedstoneSandboxError(
                SandboxErrorCode.CREATE_FAILED, safe_message=message, internal=result.stderr[:500],
            )
        return result

    def _start_egress_proxy(self, config: SandboxConfig, sandbox_id: str) -> str:
        """Build the install topology and return the proxy's address on it.

            install sandbox --(<id>-net, --internal)--> <id>-proxy --(<id>-egress)--> internet

        `<id>-net` is a Docker --internal network: it has no gateway and no
        NAT, so nothing on it can reach any address outside it (verified:
        ENETUNREACH), and Docker's embedded DNS does not resolve external
        names on it (verified: SERVFAIL), so DNS is not a side channel. Its
        only members are this sandbox and its own proxy. The proxy is the
        only container on `<id>-egress`. So the sandbox's entire reachable
        world is the proxy's CONNECT policy -- whatever it does to
        HTTP(S)_PROXY, NO_PROXY or npm's registry setting. One proxy per
        install: no shared infrastructure, no cross-workspace policy.

        Fails closed: any failure raises CREATE_FAILED before the sandbox
        exists; there is no fallback network.
        """
        policy = config.egress
        internal = self._network_name(sandbox_id)
        egress = self._egress_network_name(sandbox_id)
        proxy = self._proxy_name(sandbox_id)

        if not self._proxy_script.is_file():
            raise RedstoneSandboxError(
                SandboxErrorCode.CREATE_FAILED,
                safe_message="The install egress proxy is unavailable.",
                internal="egress proxy script missing",
            )

        self._checked(["network", "create", "--internal", "--label", f"{MANAGED_LABEL}=true",
                       internal], "The sandbox network could not be created.")
        self._checked(["network", "create", "--driver", "bridge",
                       "--label", f"{MANAGED_LABEL}=true",
                       "--opt", "com.docker.network.bridge.enable_icc=false", egress],
                      "The sandbox egress network could not be created.")

        proxy_env = dict(_BASE_ENV)
        proxy_env.update({
            "REDSTONE_EGRESS_ALLOW": ",".join(policy.allowed_hosts),
            "REDSTONE_EGRESS_PORTS": ",".join(str(p) for p in policy.allowed_ports),
            "REDSTONE_EGRESS_CONNECT_TIMEOUT_MS": str(int(policy.connect_timeout_seconds * 1000)),
            "REDSTONE_EGRESS_IDLE_TIMEOUT_MS": str(int(policy.idle_timeout_seconds * 1000)),
            "REDSTONE_EGRESS_MAX_TUNNEL_MS": str(int(policy.max_tunnel_seconds * 1000)),
            "REDSTONE_EGRESS_MAX_CONNECTIONS": str(policy.max_connections),
        })
        argv = [
            "create", "--name", proxy,
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--read-only",
            "--user", "1000:1000",
            "--pids-limit", "64",
            "--memory", "128m", "--memory-swap", "128m",
            "--cpus", "0.5",
            "--log-driver", "json-file", "--log-opt", "max-size=1m", "--log-opt", "max-file=1",
            "--label", f"{MANAGED_LABEL}=true",
            "--label", f"{_PROXY_ROLE_LABEL}=egress-proxy",
        ]
        for key, value in config.labels.items():
            if key != _PROXY_ROLE_LABEL:
                argv.extend(["--label", self._label_arg(key, value)])
        argv.extend(["--network", egress,
                     "--mount", f"type=bind,source={self._proxy_script},"
                                f"target={_PROXY_CONTAINER_PATH},readonly"])
        for key, value in proxy_env.items():
            argv.extend(["--env", f"{key}={value}"])
        argv.extend([self._image, "node", _PROXY_CONTAINER_PATH])

        self._checked(argv, "The install egress proxy could not be created.")
        self._checked(["network", "connect", internal, proxy],
                      "The install egress proxy could not be attached.")
        self._checked(["start", proxy], "The install egress proxy could not be started.")
        self._wait_for_proxy(proxy, policy.startup_timeout_seconds)

        inspected = self._checked(
            ["inspect", "-f", "{{(index .NetworkSettings.Networks \"" + internal + "\").IPAddress}}",
             proxy],
            "The install egress proxy has no address.",
        )
        address = inspected.stdout.strip()
        try:
            ipaddress.ip_address(address)
        except ValueError:
            raise RedstoneSandboxError(
                SandboxErrorCode.CREATE_FAILED,
                safe_message="The install egress proxy has no address.",
                internal=f"unparsable proxy address {address[:64]!r}",
            )
        return address

    def _wait_for_proxy(self, proxy: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _PROXY_READY in _run_docker(["logs", proxy], timeout=15).stdout:
                return
            running = _run_docker(["inspect", "-f", "{{.State.Running}}", proxy], timeout=15)
            if running.stdout.strip() != "true":
                raise RedstoneSandboxError(
                    SandboxErrorCode.CREATE_FAILED,
                    safe_message="The install egress proxy failed to start.",
                    internal="proxy exited before becoming ready",
                )
            time.sleep(0.1)
        raise RedstoneSandboxError(
            SandboxErrorCode.CREATE_FAILED,
            safe_message="The install egress proxy did not become ready.",
            internal="proxy startup timeout",
        )

    def _remove_network(self, sandbox_id: str) -> None:
        """Tear down install infrastructure: proxy, then both networks.
        Idempotent -- DENY sandboxes never had any of it."""
        for argv in (["rm", "-f", self._proxy_name(sandbox_id)],
                     ["network", "rm", self._network_name(sandbox_id)],
                     ["network", "rm", self._egress_network_name(sandbox_id)]):
            try:
                _run_docker(argv, timeout=30)
            except RedstoneSandboxError:
                pass

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
        self._start_storage_watchdog(sandbox_id)

    # --------------------------------------------------- storage watchdog

    def _start_storage_watchdog(self, sandbox_id: str) -> None:
        with self._lock:
            info = self._sandboxes.get(sandbox_id)
            if info is None or not info["watch_paths"] or info["storage_mb"] <= 0:
                return
            if info["stop_event"] is not None:
                return
            stop_event = threading.Event()
            info["stop_event"] = stop_event
            watch_paths = info["watch_paths"]
            limit_bytes = info["storage_mb"] * 1024 * 1024

        thread = threading.Thread(
            target=self._watch_storage,
            args=(sandbox_id, watch_paths, limit_bytes, stop_event),
            name=f"storage-watchdog-{sandbox_id}",
            daemon=True,
        )
        thread.start()

    def _watch_storage(self, sandbox_id: str, watch_paths, limit_bytes: int,
                       stop_event: threading.Event) -> None:
        while not stop_event.wait(self._storage_poll_interval):
            used = sum(_directory_size(Path(p)) for p in watch_paths)
            if used <= limit_bytes:
                continue
            with self._lock:
                info = self._sandboxes.get(sandbox_id)
                if info is not None:
                    info["limit_exceeded"] = "storage_mb"
            try:
                self.kill(sandbox_id)
            except RedstoneSandboxError:
                pass
            return

    def _stop_watchdog(self, sandbox_id: str) -> None:
        with self._lock:
            info = self._sandboxes.get(sandbox_id)
            event = info.get("stop_event") if info else None
        if event is not None:
            event.set()

    def _limit_exceeded(self, sandbox_id: str) -> str | None:
        with self._lock:
            info = self._sandboxes.get(sandbox_id)
            return info.get("limit_exceeded") if info else None

    def _output_bytes(self, sandbox_id: str) -> int:
        with self._lock:
            info = self._sandboxes.get(sandbox_id)
            return info["output_bytes"] if info else 256 * 1024

    # --------------------------------------------------------------- wait

    def wait(self, sandbox_id: str, timeout: float) -> SandboxResult:
        started = time.monotonic()
        try:
            wait_result = subprocess.run(
                ["docker", "wait", sandbox_id],
                capture_output=True, text=True, timeout=timeout,
            )
            timed_out = False
            raw = wait_result.stdout.strip()
            exit_code = int(raw) if raw.lstrip("-").isdigit() else None
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
            # The whole point of this method: on timeout, terminate the
            # ENTIRE process tree. `docker kill` sends SIGKILL to PID 1 of
            # the container's own cgroup/PID namespace, which the kernel
            # tears down completely -- every descendant goes with it, not
            # just the top-level process. Verified directly in
            # test_timeout_terminates_the_entire_process_tree.
            self.kill(sandbox_id)

        self._stop_watchdog(sandbox_id)
        duration = time.monotonic() - started
        stdout, out_truncated, stderr, err_truncated, _code, _log_timeout = _read_bounded_split(
            ["logs", sandbox_id], self._output_bytes(sandbox_id), _LOG_READ_TIMEOUT_SECONDS,
        )
        return SandboxResult(
            exit_code=exit_code, stdout=stdout, stderr=stderr,
            truncated=out_truncated or err_truncated,
            timed_out=timed_out, duration_seconds=duration,
            resource_limit_exceeded=self._limit_exceeded(sandbox_id),
        )

    # ------------------------------------------------------------- exec_in

    def exec_in(self, sandbox_id: str, argv: tuple[str, ...], timeout: float) -> SandboxResult:
        started = time.monotonic()
        stdout, out_t, stderr, err_t, exit_code, timed_out = _read_bounded_split(
            ["exec", sandbox_id, *argv], 8192, timeout,
        )
        # An exec that outlived its timeout is reported as timed out with no
        # exit code -- never as success.
        return SandboxResult(
            exit_code=exit_code, stdout=stdout, stderr=stderr,
            truncated=out_t or err_t, timed_out=timed_out,
            duration_seconds=time.monotonic() - started,
        )

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
            enforced_limits=("cpu_cores", "memory_mb", "pids", "timeout_seconds",
                             "output_bytes", "storage_mb"),
            resource_limit_exceeded=self._limit_exceeded(sandbox_id)
            or ("memory_mb" if state_info.get("OOMKilled") else None),
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
        """Merged, chronologically interleaved output for human diagnosis.
        Structured callers that need separate streams and an exact
        truncation flag use wait()'s SandboxResult instead."""
        text, _truncated = _read_bounded(["logs", sandbox_id], max_bytes,
                                         timeout=_LOG_READ_TIMEOUT_SECONDS)
        return text

    # ------------------------------------------------------ list_managed

    def list_managed(self) -> tuple[dict, ...]:
        """Every container carrying redstone.managed=true, found by asking
        Docker itself -- never from this object's own memory. The label
        filter is applied by the Docker CLI, so an unlabelled container
        cannot appear here at all."""
        result = _run_docker(
            ["ps", "-a", "--filter", f"label={MANAGED_LABEL}=true", "--format", "{{.Names}}"],
            timeout=15,
        )
        if result.returncode != 0:
            raise RedstoneSandboxError(
                SandboxErrorCode.PROVIDER_UNAVAILABLE, internal=result.stderr[:500]
            )
        names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not names:
            return ()

        inspected = _run_docker(["inspect", *names], timeout=30)
        if inspected.returncode != 0:
            # A container vanished between ps and inspect; retry one by one.
            entries = []
            for name in names:
                single = _run_docker(["inspect", name], timeout=15)
                if single.returncode == 0:
                    entries.extend(json.loads(single.stdout))
        else:
            entries = json.loads(inspected.stdout)

        managed = []
        for entry in entries:
            labels = (entry.get("Config") or {}).get("Labels") or {}
            if labels.get(MANAGED_LABEL) != "true":
                continue   # belt and braces: never trust the filter alone
            managed.append({
                "sandbox_id": (entry.get("Name") or "").lstrip("/"),
                "labels": {k: v for k, v in labels.items() if k.startswith(_LABEL_PREFIX)},
                "state": self._map_state(entry.get("State") or {}).value,
            })
        return tuple(managed)

    # --------------------------------------------------------------- stop

    def stop(self, sandbox_id: str, timeout: float) -> None:
        self._stop_watchdog(sandbox_id)
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
        self._stop_watchdog(sandbox_id)
        result = _run_docker(["rm", "-f", sandbox_id], timeout=30)
        if result.returncode != 0 and "No such container" not in result.stderr:
            raise RedstoneSandboxError(
                SandboxErrorCode.DESTROY_FAILED, internal=result.stderr[:500]
            )
        self._remove_network(sandbox_id)
        with self._lock:
            self._sandboxes.pop(sandbox_id, None)
