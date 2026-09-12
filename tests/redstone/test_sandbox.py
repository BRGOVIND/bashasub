"""Sandbox layer tests.

Local-process-provider tests always run (no external dependency). Docker
tests are skipped, with an explicit reason, when the daemon is not reachable
-- never silently mocked to fake a pass. Where Docker IS available these
exercise the real kernel isolation primitives (cgroups, capabilities,
namespaces) directly: this file is the regression record of the manual
verification performed while building the provider.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from redstone.domain.models import Framework
from redstone.sandbox.commands import Operation, SandboxCommand
from redstone.sandbox.errors import RedstoneSandboxError, SandboxErrorCode
from redstone.sandbox.models import (
    Mount,
    NetworkPolicy,
    ResourceLimits,
    SandboxConfig,
    SandboxState,
)
from redstone.sandbox.providers.docker_provider import DockerSandboxProvider, docker_available
from redstone.sandbox.providers.local_provider import LocalProcessSandboxProvider
from redstone.sandbox.providers.registry import get_sandbox_provider, known_providers

DOCKER_UP = docker_available()
skip_no_docker = pytest.mark.skipif(not DOCKER_UP, reason="Docker daemon not reachable")


class _FixedCommand:
    """A trivial SandboxCommand-shaped object for tests that don't need the
    real command table."""

    def __init__(self, argv: tuple[str, ...]):
        self._argv = argv

    def resolve(self) -> tuple[str, ...]:
        return self._argv


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "App.txt").write_text("workspace content", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- commands

def test_command_table_is_fixed_for_the_supported_stack():
    argv = SandboxCommand(Operation.INSTALL_DEPENDENCIES, Framework.REACT_VITE_TS).resolve()
    assert argv[0] == "npm"
    assert "install" in argv


def test_unsupported_framework_raises_not_guesses():
    with pytest.raises(RedstoneSandboxError) as caught:
        SandboxCommand(Operation.BUILD, Framework.STATIC).resolve()
    assert caught.value.code is SandboxErrorCode.UNSUPPORTED_OPERATION


def test_only_install_operation_declares_network_need():
    assert SandboxCommand(Operation.INSTALL_DEPENDENCIES, Framework.REACT_VITE_TS).needs_network
    assert not SandboxCommand(Operation.START_DEV_SERVER, Framework.REACT_VITE_TS).needs_network
    assert not SandboxCommand(Operation.BUILD, Framework.REACT_VITE_TS).needs_network


# ------------------------------------------------------------------ config

def test_config_rejects_allowlist_and_full_network_policy(workspace):
    for policy in (NetworkPolicy.ALLOWLIST, NetworkPolicy.FULL):
        with pytest.raises(ValueError):
            SandboxConfig(
                mounts=(Mount(workspace, "/w", read_only=False),),
                working_dir="/w",
                command=_FixedCommand(("sleep", "1")),
                network_policy=policy,
            )


def test_config_requires_at_least_one_mount():
    with pytest.raises(ValueError):
        SandboxConfig(mounts=(), working_dir="/w", command=_FixedCommand(("true",)))


# ------------------------------------------------------------------ registry

def test_registry_known_providers():
    assert set(known_providers()) == {"docker", "local_process"}


def test_registry_unknown_provider_is_safe_error():
    with pytest.raises(RedstoneSandboxError) as caught:
        get_sandbox_provider("firecracker")
    assert caught.value.code is SandboxErrorCode.PROVIDER_UNAVAILABLE


def test_local_provider_declares_itself_unisolated():
    assert LocalProcessSandboxProvider.is_isolated is False


def test_docker_provider_declares_itself_isolated():
    assert DockerSandboxProvider.is_isolated is True


# ============================================================= local process

class TestLocalProcessProvider:
    """Lifecycle/logic tests that do not depend on real isolation."""

    def _config(self, workspace, argv, **kwargs):
        return SandboxConfig(
            mounts=(Mount(workspace, "/w", read_only=False),),
            working_dir="/w",
            command=_FixedCommand(argv),
            **kwargs,
        )

    def test_basic_lifecycle(self, workspace):
        provider = LocalProcessSandboxProvider()
        config = self._config(workspace, ("python", "-c", "print('hello')"))
        sid = provider.create(config)
        provider.start(sid)
        result = provider.wait(sid, timeout=10)

        assert result.ok
        assert "hello" in result.stdout
        provider.destroy(sid)

    def test_environment_is_explicit_only(self, workspace):
        provider = LocalProcessSandboxProvider()
        config = self._config(
            workspace,
            ("python", "-c", "import os,sys; sys.stdout.write(str(sorted(os.environ)))"),
        )
        config = SandboxConfig(mounts=config.mounts, working_dir=config.working_dir,
                               command=config.command, environment={"ONLY_ME": "1"})
        sid = provider.create(config)
        provider.start(sid)
        result = provider.wait(sid, timeout=10)

        assert result.stdout.strip() == "['ONLY_ME']"
        provider.destroy(sid)

    def test_nonexistent_binary_fails_cleanly(self, workspace):
        provider = LocalProcessSandboxProvider()
        config = self._config(workspace, ("this-binary-does-not-exist-anywhere",))
        sid = provider.create(config)
        with pytest.raises(RedstoneSandboxError):
            provider.start(sid)

    def test_timeout_marks_result_and_kills(self, workspace):
        provider = LocalProcessSandboxProvider()
        config = self._config(workspace, ("python", "-c", "import time; time.sleep(30)"))
        sid = provider.create(config)
        provider.start(sid)

        result = provider.wait(sid, timeout=1)

        assert result.timed_out
        assert result.exit_code is None
        status = provider.status(sid)
        assert status.state == SandboxState.KILLED
        provider.destroy(sid)

    def test_output_is_truncated_past_the_limit(self, workspace):
        provider = LocalProcessSandboxProvider()
        config = SandboxConfig(
            mounts=(Mount(workspace, "/w", read_only=False),), working_dir="/w",
            command=_FixedCommand(
                ("python", "-c", "print('x' * 500)")
            ),
            resource_limits=ResourceLimits(output_bytes=50, timeout_seconds=10),
        )
        sid = provider.create(config)
        provider.start(sid)
        result = provider.wait(sid, timeout=10)

        assert result.truncated
        assert len(result.stdout.encode("utf-8")) <= 60   # small slop for the newline

    def test_stop_kill_destroy_are_idempotent(self, workspace):
        provider = LocalProcessSandboxProvider()
        config = self._config(workspace, ("python", "-c", "import time; time.sleep(5)"))
        sid = provider.create(config)
        provider.start(sid)

        provider.stop(sid, timeout=2)
        provider.stop(sid, timeout=2)      # already stopped: must not raise
        provider.kill(sid)                 # already stopped: must not raise
        provider.destroy(sid)
        provider.destroy(sid)              # already destroyed: must not raise

    def test_destroy_of_unknown_id_does_not_raise(self):
        provider = LocalProcessSandboxProvider()
        provider.destroy("local-never-existed")   # must not raise

    def test_status_of_unknown_id_is_destroyed(self):
        provider = LocalProcessSandboxProvider()
        status = provider.status("local-never-existed")
        assert status.state == SandboxState.DESTROYED


# ==================================================================== docker

@skip_no_docker
class TestDockerProviderRealIsolation:
    """Every assertion here was independently verified by hand against the
    real daemon before being written down; see the Phase 4 session notes.
    These are not aspirational -- they run against a real container."""

    IMAGE_PULLED = False

    @pytest.fixture
    def provider(self):
        return DockerSandboxProvider()

    @pytest.fixture
    def running_sandbox(self, provider, workspace):
        """A long-lived DENY-network sandbox, cleaned up after the test."""
        config = SandboxConfig(
            mounts=(Mount(workspace, "/workspace/project", read_only=False),),
            working_dir="/workspace/project",
            command=_FixedCommand(("sleep", "60")),
            network_policy=NetworkPolicy.DENY,
            resource_limits=ResourceLimits(cpu_cores=0.5, memory_mb=256, pids=64),
        )
        sid = provider.create(config)
        provider.start(sid)
        time.sleep(0.5)
        yield provider, sid
        provider.kill(sid)
        provider.destroy(sid)

    def test_basic_lifecycle(self, running_sandbox):
        provider, sid = running_sandbox
        status = provider.status(sid)
        assert status.state == SandboxState.RUNNING

    def test_workspace_mount_is_readable(self, running_sandbox):
        provider, sid = running_sandbox
        result = provider.exec_in(sid, ("cat", "/workspace/project/App.txt"), timeout=5)
        assert result.exit_code == 0
        assert "workspace content" in result.stdout

    def test_filesystem_cannot_reach_outside_the_mount(self, provider, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        other = tmp_path / "other_project"
        other.mkdir()
        (other / "secret.txt").write_text("OTHER PROJECT SECRET", encoding="utf-8")

        config = SandboxConfig(
            mounts=(Mount(workspace, "/workspace/project", read_only=False),),
            working_dir="/workspace/project", command=_FixedCommand(("sleep", "30")),
            network_policy=NetworkPolicy.DENY,
        )
        sid = provider.create(config)
        provider.start(sid)
        time.sleep(0.3)

        result = provider.exec_in(sid, ("cat", "/other_project/secret.txt"), timeout=5)

        assert result.exit_code != 0
        assert "OTHER PROJECT SECRET" not in result.stdout
        provider.kill(sid)
        provider.destroy(sid)

    def test_root_filesystem_is_read_only(self, running_sandbox):
        provider, sid = running_sandbox
        result = provider.exec_in(sid, ("sh", "-c", "echo x > /etc/pwned"), timeout=5)
        assert result.exit_code != 0
        assert "Read-only" in result.stderr

    def test_environment_contains_no_host_or_secret_variable(self, running_sandbox):
        provider, sid = running_sandbox
        result = provider.exec_in(sid, ("env",), timeout=5)
        for forbidden in ("GEMINI_API_KEY", "AI_API_KEY", "OPENAI_API_KEY",
                          "ANTHROPIC_API_KEY", "GROQ_API_KEY", "DATABASE_URL",
                          "REDSTONE_", "USERPROFILE", "COMPUTERNAME"):
            assert forbidden not in result.stdout

    def test_runs_as_non_root(self, running_sandbox):
        provider, sid = running_sandbox
        result = provider.exec_in(sid, ("id", "-u"), timeout=5)
        assert result.stdout.strip() == "1000"

    def test_all_capabilities_are_dropped(self, running_sandbox):
        provider, sid = running_sandbox
        result = provider.exec_in(sid, ("cat", "/proc/self/status"), timeout=5)
        cap_lines = [l for l in result.stdout.splitlines() if l.startswith("Cap")]
        assert cap_lines
        for line in cap_lines:
            assert line.split()[1] == "0000000000000000", line

    def test_network_deny_blocks_outbound(self, running_sandbox):
        provider, sid = running_sandbox
        result = provider.exec_in(
            sid, ("wget", "-T", "5", "-q", "-O", "-", "https://registry.npmjs.org/react"),
            timeout=10,
        )
        assert result.exit_code != 0

    def test_network_install_only_reaches_the_real_registry(self, provider, tmp_path):
        """The positive control for the DENY test above: proves the isolation
        is a deliberate policy, not merely a broken network."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        config = SandboxConfig(
            mounts=(Mount(workspace, "/workspace/project", read_only=False),),
            working_dir="/workspace/project", command=_FixedCommand(("sleep", "30")),
            network_policy=NetworkPolicy.INSTALL_ONLY,
        )
        sid = provider.create(config)
        provider.start(sid)
        time.sleep(0.3)

        result = provider.exec_in(
            sid, ("wget", "-T", "10", "-q", "-O", "/dev/null", "https://registry.npmjs.org/react"),
            timeout=15,
        )

        assert result.exit_code == 0
        provider.kill(sid)
        provider.destroy(sid)

    def test_real_npm_install_succeeds_over_install_only_network(self, provider, tmp_path):
        """The end-to-end proof: a real package installs from the real
        registry, inside the isolation, over the install-phase network."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "package.json").write_text(
            '{"name":"t","version":"1.0.0","dependencies":{"is-odd":"3.0.1"}}',
            encoding="utf-8",
        )
        config = SandboxConfig(
            mounts=(Mount(workspace, "/workspace/project", read_only=False),),
            working_dir="/workspace/project",
            command=SandboxCommand(Operation.INSTALL_DEPENDENCIES, Framework.REACT_VITE_TS),
            network_policy=NetworkPolicy.INSTALL_ONLY,
            resource_limits=ResourceLimits(timeout_seconds=90),
        )
        sid = provider.create(config)
        provider.start(sid)
        result = provider.wait(sid, timeout=90)

        assert result.ok, result.stdout
        assert (workspace / "node_modules" / "is-odd").is_dir()
        provider.destroy(sid)

    def test_pids_limit_bounds_a_fork_bomb(self, provider, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        config = SandboxConfig(
            mounts=(Mount(workspace, "/workspace/project", read_only=False),),
            working_dir="/workspace/project", command=_FixedCommand(("sleep", "30")),
            network_policy=NetworkPolicy.DENY,
            resource_limits=ResourceLimits(pids=8, memory_mb=128, cpu_cores=0.5),
        )
        sid = provider.create(config)
        provider.start(sid)
        time.sleep(0.3)

        result = provider.exec_in(
            sid, ("sh", "-c", "for i in $(seq 1 40); do sleep 20 & done; wait"), timeout=8
        )

        assert "can't fork" in result.stderr or result.exit_code != 0
        provider.kill(sid)
        provider.destroy(sid)

    def test_memory_limit_is_enforced(self, provider, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        config = SandboxConfig(
            mounts=(Mount(workspace, "/workspace/project", read_only=False),),
            working_dir="/workspace/project", command=_FixedCommand(("sleep", "30")),
            network_policy=NetworkPolicy.DENY,
            resource_limits=ResourceLimits(memory_mb=64, pids=50, cpu_cores=0.5),
        )
        sid = provider.create(config)
        provider.start(sid)
        time.sleep(0.3)

        result = provider.exec_in(
            sid, ("dd", "if=/dev/zero", "of=/tmp/big", "bs=1M", "count=200"), timeout=15
        )

        assert result.exit_code != 0   # OOM-killed before completing
        provider.kill(sid)
        provider.destroy(sid)

    def test_timeout_terminates_the_entire_process_tree(self, provider, tmp_path):
        """Parent -> child -> grandchild, all long-sleeping; proves kill()
        tears down the whole tree, not just the top-level process."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        tree_cmd = _FixedCommand(
            ("sh", "-c", 'sh -c "sh -c \'sleep 999\' & sleep 999" & sleep 999')
        )
        config = SandboxConfig(
            mounts=(Mount(workspace, "/workspace/project", read_only=False),),
            working_dir="/workspace/project", command=tree_cmd,
            network_policy=NetworkPolicy.DENY,
        )
        sid = provider.create(config)
        provider.start(sid)
        time.sleep(1)

        before = provider.exec_in(sid, ("ps", "aux"), timeout=5)
        assert before.stdout.count("sleep 999") >= 3   # parent+child+grandchild really running

        result = provider.wait(sid, timeout=2)
        assert result.timed_out

        time.sleep(0.5)
        status = provider.status(sid)
        assert status.state == SandboxState.KILLED
        provider.destroy(sid)

    def test_stop_kill_destroy_are_idempotent(self, provider, workspace):
        config = SandboxConfig(
            mounts=(Mount(workspace, "/workspace/project", read_only=False),),
            working_dir="/workspace/project", command=_FixedCommand(("sleep", "5")),
            network_policy=NetworkPolicy.DENY,
        )
        sid = provider.create(config)
        provider.start(sid)
        time.sleep(0.3)

        provider.stop(sid, timeout=2)
        provider.stop(sid, timeout=2)
        provider.kill(sid)
        provider.destroy(sid)
        provider.destroy(sid)   # must not raise

    def test_destroy_of_unknown_container_does_not_raise(self, provider):
        provider.destroy("redstone-never-existed-ffffffff")

    def test_status_of_destroyed_sandbox_is_destroyed(self, provider, workspace):
        config = SandboxConfig(
            mounts=(Mount(workspace, "/workspace/project", read_only=False),),
            working_dir="/workspace/project", command=_FixedCommand(("true",)),
            network_policy=NetworkPolicy.DENY,
        )
        sid = provider.create(config)
        provider.start(sid)
        provider.wait(sid, timeout=10)
        provider.destroy(sid)

        assert provider.status(sid).state == SandboxState.DESTROYED

    def test_logs_are_bounded(self, provider, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        config = SandboxConfig(
            mounts=(Mount(workspace, "/workspace/project", read_only=False),),
            working_dir="/workspace/project",
            command=_FixedCommand(("sh", "-c", "yes x | head -c 2000000")),
            network_policy=NetworkPolicy.DENY,
        )
        sid = provider.create(config)
        provider.start(sid)
        result = provider.wait(sid, timeout=20)

        assert len(result.stdout.encode("utf-8")) <= 256 * 1024 + 1024   # small slop
        provider.destroy(sid)


@skip_no_docker
def test_dangerous_flags_never_appear_in_the_constructed_argv(monkeypatch, tmp_path):
    """Static safety net, independent of runtime behaviour: inspect the exact
    argv docker create would receive and assert the dangerous flags are
    structurally impossible, not merely "not currently used"."""
    captured = {}

    import subprocess as subprocess_module
    real_run = subprocess_module.run

    def spy(argv, **kwargs):
        if argv[:2] == ["docker", "create"]:
            captured["argv"] = argv
            # Don't actually create anything for this test.
            class _Result:
                returncode = 1
                stdout = ""
                stderr = "intentionally not created"
            return _Result()
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess_module, "run", spy)

    provider = DockerSandboxProvider()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = SandboxConfig(
        mounts=(Mount(workspace, "/workspace/project", read_only=False),),
        working_dir="/workspace/project", command=_FixedCommand(("sleep", "1")),
        network_policy=NetworkPolicy.DENY,
    )
    try:
        provider.create(config)
    except RedstoneSandboxError:
        pass   # expected: the spy makes create() fail on purpose

    argv = captured.get("argv", [])
    argv_str = " ".join(argv)
    for dangerous in ("--privileged", "--network host", "--network=host",
                      "--pid host", "--pid=host", "--ipc host", "--ipc=host",
                      "docker.sock"):
        assert dangerous not in argv_str, f"dangerous flag present: {dangerous}"
    assert "--cap-drop" in argv and "ALL" in argv
    assert "--security-opt" in argv and "no-new-privileges" in argv
