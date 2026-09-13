"""Phase 4.1 sandbox hardening -- every test here runs against the REAL
Docker daemon (skipped, with an explicit reason, when none is reachable).

Each security claim in docs/redstone/SANDBOX.md added or corrected in
Phase 4.1 has a test in this file that drives the real mechanism and
observes the real outcome: storage quota, bounded tmpfs, output truncation
and stream separation, CPU throttling, the kernel no_new_privs bit, device
posture, in-sandbox symlink/hardlink attempts, targeted network
destinations under both policies, and a hostile npm lifecycle script.

Tests named test_*_is_not_* are CHARACTERIZATION tests: they assert a
documented, still-open gap exists exactly as documented, so the day it is
closed (or silently widens) the suite says so.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
import time
import uuid
from pathlib import Path

import pytest

from redstone.domain.models import Framework
from redstone.sandbox.commands import Operation, SandboxCommand
from redstone.sandbox.errors import RedstoneSandboxError
from redstone.sandbox.models import (
    Mount,
    NetworkPolicy,
    ResourceLimits,
    SandboxConfig,
    SandboxState,
)
from redstone.sandbox.providers import docker_provider as docker_provider_module
from redstone.sandbox.providers.docker_provider import (
    DEFAULT_IMAGE,
    MANAGED_LABEL,
    DockerSandboxProvider,
    _directory_size,
    docker_available,
)

DOCKER_UP = docker_available()
pytestmark = pytest.mark.skipif(not DOCKER_UP, reason="Docker daemon not reachable")

FIXTURES = Path(__file__).parent / "fixtures"
MB = 1024 * 1024


class _FixedCommand:
    """A SandboxCommand-shaped object for tests that need a literal argv."""

    def __init__(self, argv: tuple[str, ...]):
        self._argv = argv

    def resolve(self) -> tuple[str, ...]:
        return self._argv


def _config(project: Path, argv, *, policy=NetworkPolicy.DENY, limits=None, env=None, labels=None):
    return SandboxConfig(
        mounts=(Mount(project, "/workspace/project", read_only=False),),
        working_dir="/workspace/project",
        command=argv if hasattr(argv, "resolve") else _FixedCommand(tuple(argv)),
        network_policy=policy,
        resource_limits=limits or ResourceLimits(),
        environment=env or {},
        labels=labels or {},
    )


@pytest.fixture
def docker():
    """A real provider whose every sandbox is destroyed after the test,
    whatever happened during it."""
    provider = DockerSandboxProvider(storage_poll_interval=0.2)
    created: list[str] = []
    real_create = provider.create

    def tracking_create(config):
        sandbox_id = real_create(config)
        created.append(sandbox_id)
        return sandbox_id

    provider.create = tracking_create
    yield provider
    for sandbox_id in created:
        try:
            provider.destroy(sandbox_id)
        except RedstoneSandboxError:
            pass


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "ws"
    (root / "project").mkdir(parents=True)
    return root / "project"


def _run(provider, config, timeout=60):
    sandbox_id = provider.create(config)
    provider.start(sandbox_id)
    return sandbox_id, provider.wait(sandbox_id, timeout=timeout)


def _long_running(provider, config):
    sandbox_id = provider.create(config)
    provider.start(sandbox_id)
    time.sleep(0.5)
    return sandbox_id


# ========================================================== BLOCKER 1: storage

def test_storage_quota_kills_a_process_writing_directly_to_the_mount(docker, project):
    """The writer is the untrusted process itself (dd inside the container),
    writing straight into the bind-mounted project directory -- no Redstone
    Python file API is anywhere on this path."""
    limit_mb = 8
    writer = ("sh", "-c",
              "i=0; while true; do dd if=/dev/zero of=blob$i bs=1M count=2 2>/dev/null; "
              "i=$((i+1)); done")
    config = _config(project, writer, limits=ResourceLimits(storage_mb=limit_mb))

    sandbox_id = docker.create(config)
    started = time.monotonic()
    docker.start(sandbox_id)

    # Redstone stays responsive while the watchdog works.
    probe_started = time.monotonic()
    docker.status(sandbox_id)
    assert time.monotonic() - probe_started < 10

    result = docker.wait(sandbox_id, timeout=90)
    elapsed = time.monotonic() - started
    used = _directory_size(project)

    assert not result.timed_out, "the writer must be stopped by the quota, not the timeout"
    assert result.resource_limit_exceeded == "storage_mb"
    status = docker.status(sandbox_id)
    assert status.state is SandboxState.KILLED
    assert status.resource_limit_exceeded == "storage_mb"
    assert used > limit_mb * MB            # the ceiling was genuinely reached ...
    assert used < limit_mb * MB + 256 * MB   # ... and the process could not keep going
    assert elapsed < 60

    # Cleanup still works after a quota kill.
    docker.destroy(sandbox_id)
    assert docker.status(sandbox_id).state is SandboxState.DESTROYED


def test_storage_quota_does_not_fire_under_the_limit(docker, project):
    config = _config(project, ("sh", "-c", "dd if=/dev/zero of=small bs=1M count=1 2>/dev/null"),
                     limits=ResourceLimits(storage_mb=64))
    _, result = _run(docker, config)
    assert result.ok
    assert result.resource_limit_exceeded is None


def test_tmpfs_is_bounded_by_the_storage_limit(docker, project):
    """Without size=, Docker's tmpfs may grow to half of host RAM -- a second,
    uncounted storage channel. It is now capped at storage_mb."""
    config = _config(project, ("sh", "-c", "dd if=/dev/zero of=/tmp/fill bs=1M count=64"),
                     limits=ResourceLimits(storage_mb=16))
    _, result = _run(docker, config)
    assert result.exit_code != 0
    assert "No space left on device" in result.stderr


# ================================================ BLOCKER 5 + stdout/stderr

OUT_LIMIT = 4096


def _output(docker, project, script):
    config = _config(project, ("sh", "-c", script), limits=ResourceLimits(output_bytes=OUT_LIMIT))
    return _run(docker, config)[1]


def test_output_below_the_limit_is_not_truncated(docker, project):
    result = _output(docker, project, "printf 'hello\\n'")
    assert result.stdout == "hello\n"
    assert result.truncated is False


def test_output_exactly_at_the_limit_is_not_truncated(docker, project):
    # 4095 bytes + newline == exactly OUT_LIMIT bytes.
    result = _output(docker, project, "head -c 4095 /dev/zero | tr '\\0' x; printf '\\n'")
    assert len(result.stdout.encode()) == OUT_LIMIT
    assert result.truncated is False


def test_output_one_line_over_the_limit_is_truncated(docker, project):
    result = _output(docker, project, "head -c 8191 /dev/zero | tr '\\0' x; printf '\\n'")
    assert result.truncated is True
    assert len(result.stdout.encode()) <= OUT_LIMIT


def test_stdout_flood_is_bounded_and_flagged(docker, project):
    result = _output(docker, project, "yes x | head -c 2000000")
    assert result.truncated is True
    assert len(result.stdout.encode()) <= OUT_LIMIT
    assert result.stderr == ""


def test_stderr_flood_is_bounded_and_flagged(docker, project):
    result = _output(docker, project, "yes x | head -c 2000000 1>&2")
    assert result.truncated is True
    assert len(result.stderr.encode()) <= OUT_LIMIT
    assert result.stdout == ""


def test_stdout_and_stderr_are_genuinely_separate(docker, project):
    result = _output(docker, project, "echo ON_STDOUT; echo ON_STDERR 1>&2")
    assert result.stdout == "ON_STDOUT\n"
    assert result.stderr == "ON_STDERR\n"
    assert result.truncated is False


# ============================================================ BLOCKER 4: CPU

def _cpu_share(docker, project, cores: float, window: float = 4.0) -> float:
    """CPU-seconds consumed by a single-threaded busy loop, divided by the
    wall-clock window, read from the kernel's own per-process accounting
    (/proc/1/stat utime+stime) -- not from how much 'work' got done."""
    config = _config(project, ("sh", "-c", "while :; do :; done"),
                     limits=ResourceLimits(cpu_cores=cores))
    sandbox_id = _long_running(docker, config)
    read = ("awk", "{print $14 + $15}", "/proc/1/stat")
    clk = int(docker.exec_in(sandbox_id, ("getconf", "CLK_TCK"), timeout=10).stdout.strip())
    t0 = int(docker.exec_in(sandbox_id, read, timeout=10).stdout.strip())
    w0 = time.monotonic()
    time.sleep(window)
    t1 = int(docker.exec_in(sandbox_id, read, timeout=10).stdout.strip())
    w1 = time.monotonic()
    docker.kill(sandbox_id)
    return ((t1 - t0) / clk) / (w1 - w0)


def test_cpu_limit_is_behaviourally_enforced(docker, project):
    capped = _cpu_share(docker, project, cores=0.5)
    control = _cpu_share(docker, project, cores=2.0)   # single thread can use one full core

    # The cap holds the busy loop near half a core ...
    assert 0.2 < capped < 0.75, f"capped share {capped:.2f}"
    # ... and the measurement is able to see more than that when allowed,
    # so the low number is the cap, not a broken measurement.
    assert control > capped * 1.4, f"control {control:.2f} vs capped {capped:.2f}"


# ================================================ BLOCKER 4: no-new-privileges

def test_no_new_privs_bit_is_set_on_the_sandboxed_process(docker, project):
    """Reads the kernel's own per-task state, not the argv: NoNewPrivs: 1
    means execve() of a setuid/setgid/file-capability binary cannot raise
    this process's privileges. This verifies the kernel state the flag
    produces; it does not perform an escalation attempt (see SANDBOX.md)."""
    sandbox_id = _long_running(docker, _config(project, ("sleep", "30")))
    status = docker.exec_in(sandbox_id, ("cat", "/proc/self/status"), timeout=10).stdout
    fields = dict(line.split(":\t", 1) for line in status.splitlines() if ":\t" in line)
    assert fields["NoNewPrivs"].strip() == "1"
    assert fields["Seccomp"].strip() == "2"        # Docker's default seccomp filter is active
    assert fields["CapEff"].strip() == "0000000000000000"


# ======================================================== device posture

def test_device_posture(docker, project):
    sandbox_id = _long_running(docker, _config(project, ("sleep", "30")))

    listing = docker.exec_in(sandbox_id, ("ls", "-1", "/dev"), timeout=10).stdout.split()
    assert set(listing) <= {
        "core", "fd", "full", "mqueue", "null", "ptmx", "pts", "random", "shm",
        "stderr", "stdin", "stdout", "tty", "urandom", "zero", "console",
    }, listing

    block = docker.exec_in(sandbox_id, ("find", "/dev", "-type", "b"), timeout=10)
    assert block.stdout.strip() == ""

    for dangerous in ("/dev/mem", "/dev/kmem", "/dev/kmsg", "/dev/sda", "/dev/nvme0n1"):
        probe = docker.exec_in(sandbox_id, ("test", "-e", dangerous), timeout=10)
        assert probe.exit_code != 0, dangerous

    # CAP_MKNOD is dropped: creating a device node (here: another /dev/null) fails.
    mknod = docker.exec_in(sandbox_id, ("mknod", "/tmp/devnull", "c", "1", "3"), timeout=10)
    assert mknod.exit_code != 0


# ================================================== filesystem attack tests

CANARY = f"REDSTONE-HOST-CANARY-{uuid.uuid4().hex}"


def test_symlinks_inside_the_sandbox_cannot_reach_the_host(docker, project):
    """Container-side isolation, distinct from Phase 2A.1's host-side path
    checks: every path a symlink can name is resolved inside the container's
    own mount namespace, where the host's files do not exist."""
    host_secret = project.parent / "outside-secret.txt"
    host_secret.write_text(CANARY, encoding="utf-8")

    script = (
        f"ln -s .. /tmp/up; cat /tmp/up/outside-secret.txt; "
        f"ln -s / /tmp/root; cat /tmp/root/workspace/outside-secret.txt; "
        f"ln -s '{host_secret}' /tmp/hostpath; cat /tmp/hostpath; "
        f"ln -s .. /workspace/project/up 2>&1; cat /workspace/project/up/outside-secret.txt; "
        # A Linux symlink written onto a Windows bind mount becomes an entry
        # the Windows host cannot delete (see SANDBOX.md, Known limitations);
        # remove it from inside, where it is an ordinary symlink.
        f"rm -f /workspace/project/up; echo DONE"
    )
    _, result = _run(docker, _config(project, ("sh", "-c", script)))

    assert "DONE" in result.stdout
    assert CANARY not in result.stdout
    assert CANARY not in result.stderr
    assert host_secret.read_text(encoding="utf-8") == CANARY   # untouched


def test_workspace_with_sandbox_created_links_can_still_be_destroyed(docker, tmp_path):
    """Found during Phase 4.1: on a Windows host, links a sandbox writes onto
    the bind mount become entries plain shutil.rmtree cannot remove, which
    left real npm projects (node_modules/.bin/*) undeletable. Cleanup must
    keep working whatever the sandbox wrote. On Linux hosts this passes
    trivially -- the links are ordinary symlinks there."""
    from redstone.workspace.manager import WorkspaceManager

    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create("ws_links")
    (workspace.project_root / "src").mkdir()
    (workspace.project_root / "src" / "App.tsx").write_text("x", encoding="utf-8")
    outside = tmp_path / "must-survive.txt"
    outside.write_text(CANARY, encoding="utf-8")

    script = ("ln -s .. up; ln -s /etc etclink; ln -s src/App.tsx applink; "
              "mkdir -p node_modules/.bin && ln -s ../pkg/cli.js node_modules/.bin/tool; "
              "echo LINKED")
    _, result = _run(docker, _config(workspace.project_root, ("sh", "-c", script)))
    assert "LINKED" in result.stdout

    manager.destroy(workspace.id)

    assert not workspace.root.exists()
    assert outside.read_text(encoding="utf-8") == CANARY   # only links removed, never targets
    assert (tmp_path / "workspaces").is_dir()


def test_hardlinks_inside_the_sandbox_cannot_capture_files_outside_the_mount(docker, project):
    script = (
        "ln /etc/passwd /workspace/project/pw 2>&1; echo PW=$?; "
        "ln /etc/shadow /workspace/project/sh 2>&1; echo SH=$?; "
        "ln /usr/local/bin/node /workspace/project/nd 2>&1; echo ND=$?"
    )
    _, result = _run(docker, _config(project, ("sh", "-c", script)))

    for marker in ("PW=", "SH=", "ND="):
        line = next(line for line in result.stdout.splitlines() if line.startswith(marker))
        assert line != f"{marker}0", result.stdout
    for name in ("pw", "sh", "nd"):
        assert not (project / name).exists()


# ======================================================= network attack tests

def _wget(docker, sandbox_id, url):
    return docker.exec_in(sandbox_id, ("wget", "-T", "3", "-q", "-O", "-", url), timeout=10)


@pytest.mark.parametrize("target", [
    "http://172.17.0.1/",          # Docker default-bridge gateway (the host side)
    "http://10.0.0.1/",            # RFC1918
    "http://192.168.1.1/",         # RFC1918
    "http://169.254.169.254/",     # link-local cloud metadata address
    "https://registry.npmjs.org/", # public internet
])
def test_deny_policy_has_no_route_to_anything(docker, project, target):
    sandbox_id = _long_running(docker, _config(project, ("sleep", "30")))
    result = _wget(docker, sandbox_id, target)
    assert result.exit_code != 0
    # "unreachable"/"bad address": no route or no resolver at all -- the
    # sandbox has no network device, not a firewall rule that can be raced.
    assert ("unreachable" in result.stderr.lower()
            or "bad address" in result.stderr.lower()), result.stderr


def test_deny_policy_loopback_is_the_containers_own_and_empty(docker, project):
    """127.0.0.1 inside the sandbox is the sandbox's own loopback, not the
    host's: nothing listens there, so the connection is refused."""
    sandbox_id = _long_running(docker, _config(project, ("sleep", "30")))
    result = _wget(docker, sandbox_id, "http://127.0.0.1:8000/")
    assert result.exit_code != 0
    assert "refused" in result.stderr.lower()


NODE_CONNECT = (
    "const [h,p]=process.argv.slice(1);const s=require('net').connect(+p,h);"
    "s.setTimeout(3000,()=>{console.log('TIMEOUT');process.exit(2)});"
    "s.on('connect',()=>{console.log('CONNECTED');process.exit(0)})"
    ".on('error',e=>{console.log(e.code);process.exit(1)})"
)


@pytest.fixture
def other_container():
    """An unrelated container on Docker's DEFAULT bridge, listening on 8080
    -- standing in for, say, a developer's local database."""
    name = f"rs-hardening-target-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "alpine:3.19", "sh", "-c",
         "while true; do echo hi | nc -l -p 8080; done"],
        capture_output=True, text=True, check=True, timeout=60,
    )
    time.sleep(1)
    ip = subprocess.run(
        ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name],
        capture_output=True, text=True, check=True, timeout=15,
    ).stdout.strip()
    try:
        yield ip
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


def test_install_network_cannot_reach_other_containers(docker, project, other_container):
    # Positive control: the target really is reachable from the default bridge.
    control = subprocess.run(
        ["docker", "run", "--rm", DEFAULT_IMAGE, "node", "-e", NODE_CONNECT,
         other_container, "8080"],
        capture_output=True, text=True, timeout=60,
    )
    assert "CONNECTED" in control.stdout, control.stdout + control.stderr

    sandbox_id = _long_running(docker, _config(project, ("sleep", "30"),
                                               policy=NetworkPolicy.INSTALL_ONLY))
    result = docker.exec_in(sandbox_id, ("node", "-e", NODE_CONNECT, other_container, "8080"),
                            timeout=15)
    assert result.exit_code != 0
    assert "CONNECTED" not in result.stdout


def test_install_network_reaches_the_npm_registry(docker, project):
    sandbox_id = _long_running(docker, _config(project, ("sleep", "30"),
                                               policy=NetworkPolicy.INSTALL_ONLY))
    result = docker.exec_in(
        sandbox_id, ("wget", "-T", "10", "-q", "-O", "/dev/null", "https://registry.npmjs.org/react"),
        timeout=20,
    )
    assert result.exit_code == 0


def test_install_network_metadata_address_is_unreachable_here(docker, project):
    """ENVIRONMENT LIMITATION: this machine is not a cloud host, so there is
    no metadata service to be blocked. This proves nothing answers there
    from the install network in THIS environment; it is not evidence that a
    real cloud metadata endpoint would be blocked (it would NOT be -- see
    test_install_network_egress_is_not_registry_restricted)."""
    sandbox_id = _long_running(docker, _config(project, ("sleep", "30"),
                                               policy=NetworkPolicy.INSTALL_ONLY))
    result = docker.exec_in(sandbox_id, ("node", "-e", NODE_CONNECT, "169.254.169.254", "80"),
                            timeout=15)
    assert "CONNECTED" not in result.stdout


def test_install_network_egress_is_not_registry_restricted(docker, project):
    """CHARACTERIZATION of a documented, open gap: during INSTALL_ONLY a
    lifecycle script can reach an arbitrary internet host, not just the npm
    registry. Registry-only egress requires an egress proxy that does not
    exist yet. If this test starts failing, the gap has closed -- update
    SECURITY.md and turn this into a negative test."""
    sandbox_id = _long_running(docker, _config(project, ("sleep", "30"),
                                               policy=NetworkPolicy.INSTALL_ONLY))
    result = docker.exec_in(sandbox_id, ("node", "-e", NODE_CONNECT, "github.com", "443"),
                            timeout=15)
    assert "CONNECTED" in result.stdout, result.stdout


def test_install_network_is_removed_with_its_sandbox(docker, project):
    sandbox_id = docker.create(_config(project, ("sleep", "5"), policy=NetworkPolicy.INSTALL_ONLY))
    network = f"{sandbox_id}-net"
    listed = subprocess.run(["docker", "network", "ls", "--format", "{{.Name}}"],
                            capture_output=True, text=True, timeout=15).stdout.split()
    assert network in listed
    docker.destroy(sandbox_id)
    listed = subprocess.run(["docker", "network", "ls", "--format", "{{.Name}}"],
                            capture_output=True, text=True, timeout=15).stdout.split()
    assert network not in listed


# ============================================== static argv (no container)

@pytest.mark.parametrize("policy", [NetworkPolicy.DENY, NetworkPolicy.INSTALL_ONLY])
def test_constructed_argv_has_every_required_flag_and_no_forbidden_one(monkeypatch, project, policy):
    captured: dict[str, list] = {"network": []}
    real_run = subprocess.run

    def spy(argv, **kwargs):
        if argv[:2] == ["docker", "create"]:
            captured["create"] = argv
            return subprocess.CompletedProcess(argv, 1, "", "intentionally not created")
        if argv[:2] == ["docker", "network"]:
            captured["network"].append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(docker_provider_module.subprocess, "run", spy)
    provider = DockerSandboxProvider()
    with pytest.raises(RedstoneSandboxError):
        provider.create(_config(project, ("sleep", "1"), policy=policy,
                                limits=ResourceLimits(storage_mb=32)))

    argv = captured["create"]
    joined = " ".join(argv)
    for required in ("--security-opt no-new-privileges", "--cap-drop ALL", "--read-only",
                     "--user 1000:1000", f"--label {MANAGED_LABEL}=true", "--pids-limit",
                     "--memory", "--memory-swap", "--cpus", "/tmp:rw,size=32m"):
        assert required in joined, required
    for forbidden in ("--privileged", "--network host", "--network=host", "--pid ", "--pid=",
                      "--ipc ", "--ipc=", "--device", "docker.sock", " -p ", "--publish",
                      "--cap-add", "--network bridge", "--userns host", "--uts host"):
        assert forbidden not in joined, forbidden
    assert DEFAULT_IMAGE in argv
    assert "@sha256:" in DEFAULT_IMAGE

    network_value = argv[argv.index("--network") + 1]
    if policy is NetworkPolicy.DENY:
        assert network_value == "none"
    else:
        assert network_value.endswith("-net")
        create_net = next(a for a in captured["network"] if a[2] == "create")
        assert "com.docker.network.bridge.enable_icc=false" in create_net
        # the network of a sandbox that failed to create is removed again
        assert any(a[2] == "rm" and a[3] == network_value for a in captured["network"])


def test_labels_cannot_forge_or_escape_the_ownership_namespace(project):
    provider = DockerSandboxProvider()
    for bad in ({"evil": "x"}, {MANAGED_LABEL: "false"}, {"redstone.runtime_id": "a\nb"},
                {"redstone.runtime_id": "x" * 200}):
        with pytest.raises(RedstoneSandboxError):
            provider.create(_config(project, ("sleep", "1"), labels=bad))


# ======================================== adversarial npm lifecycle script

def test_hostile_postinstall_script_is_contained(docker, tmp_path, monkeypatch, other_container):
    """A local, never-published package whose postinstall probes for
    secrets, host files, writable system paths, other containers and process
    limits -- run through the real `npm install` operation inside the real
    install sandbox. The script records what it could reach; every forbidden
    probe must have failed."""
    # A secret genuinely present in the TRUSTED process's environment.
    monkeypatch.setenv("GEMINI_API_KEY", CANARY)
    monkeypatch.setenv("REDSTONE_DEPLOY_SECRET", CANARY)

    ws_root = tmp_path / "ws"
    project = ws_root / "project"
    project.mkdir(parents=True)
    host_secret = ws_root / "outside-secret.txt"
    host_secret.write_text(CANARY, encoding="utf-8")

    fixture = FIXTURES / "malicious_lifecycle"
    shutil.copy(fixture / "package.json", project / "package.json")
    with tarfile.open(project / "evil-probe-1.0.0.tgz", "w:gz") as tar:
        for name in ("package.json", "probe.js"):
            tar.add(fixture / "evil-probe" / name, arcname=f"package/{name}")

    config = _config(
        project, SandboxCommand(Operation.INSTALL_DEPENDENCIES, Framework.REACT_VITE_TS),
        policy=NetworkPolicy.INSTALL_ONLY,
        limits=ResourceLimits(timeout_seconds=300, pids=128),
        # Test-only hints so the probe knows what to aim at. Neither is a
        # secret -- and neither name may look like one, or the probe's own
        # secret-name scan reports the hint itself.
        env={"PROBE_TARGET_IP": other_container, "PROBE_OUTSIDE_FILE": str(host_secret)},
    )
    _, result = _run(docker, config, timeout=300)
    assert result.ok, result.stdout[-2000:] + result.stderr[-2000:]

    report_path = project / "probe-results.json"
    assert report_path.is_file(), "the lifecycle script did run inside the sandbox"
    raw = report_path.read_text(encoding="utf-8")
    report = json.loads(raw)

    assert CANARY not in raw and CANARY not in result.stdout and CANARY not in result.stderr
    assert report["uid"] == 1000
    assert report["secret_like_env"] == [], report["secret_like_env"]
    assert "GEMINI_API_KEY" not in report["env_keys"]

    assert report["read"]["etc_shadow"]["ok"] is False
    assert report["read"]["host_secret_path"]["ok"] is False
    assert report["read"]["parent_of_mount"]["ok"] is False

    for target in ("etc", "usr_local", "workspace_parent"):
        assert report["write"][target]["ok"] is False, (target, report["write"][target])
    assert report["write"]["tmp"]["ok"] is True   # bounded tmpfs is the one scratch area

    assert report["net"]["other_container"]["ok"] is False
    assert report["net"]["metadata"]["ok"] is False   # ENVIRONMENT LIMITATION, see above

    procs = report["processes"]
    assert procs["alive"] < 128, procs     # --pids-limit held inside a lifecycle script
    assert procs["failed"] is not None, procs
    assert host_secret.read_text(encoding="utf-8") == CANARY
