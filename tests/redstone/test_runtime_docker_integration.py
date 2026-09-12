"""RuntimeManager against the real Docker daemon.

Everything in test_runtime.py verifies RuntimeManager's own orchestration
logic against a fake provider. This file proves the same RuntimeManager,
wired to the real DockerSandboxProvider, produces genuine containers, a
genuine health check over a genuine loopback probe, and leaves zero orphans
behind -- closing the gap between "the state machine is correct" and "the
actual sandbox this drives is the one Phase 4 hardened".

The fixture project (tests/redstone/fixtures/react_vite_ts_min) has zero npm
dependencies and a plain Node http server standing in for the Vite dev
server, so install+start complete in a couple of seconds. This keeps the test
fast and deterministic; it deliberately does NOT re-prove that a real Vite/
React dependency tree installs correctly over NetworkPolicy.INSTALL_ONLY --
that exact codepath (a real npm install of a real published package under
INSTALL_ONLY) is already proven in
test_sandbox.py::test_real_npm_install_succeeds_over_install_only_network.
What this file adds is everything ABOVE the sandbox layer: RuntimeManager's
create/start/health-check/stop/restart/destroy sequence driving a real
container end to end.

Skipped, with an explicit reason, when the Docker daemon is not reachable --
never silently mocked to fake a pass.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from redstone.domain.models import Framework, RuntimeState
from redstone.runtime.errors import RedstoneRuntimeError
from redstone.runtime.manager import RuntimeManager
from redstone.sandbox.providers.docker_provider import DockerSandboxProvider, docker_available
from redstone.workspace.manager import WorkspaceManager

DOCKER_UP = docker_available()
skip_no_docker = pytest.mark.skipif(not DOCKER_UP, reason="Docker daemon not reachable")

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "react_vite_ts_min"
PROJECT_ID = "prj_docker_integration"


def _workspace(tmp_path, workspace_id):
    workspace = WorkspaceManager(tmp_path / "workspaces").create(workspace_id)
    for item in FIXTURE_DIR.iterdir():
        shutil.copy(item, workspace.project_root / item.name)
    return workspace


def _manager(provider, *, max_startup_seconds=45.0, health_poll_interval=1.0):
    return RuntimeManager(
        provider,
        max_startup_seconds=max_startup_seconds,
        health_check_timeout=5.0,
        stop_grace_seconds=10.0,
        health_poll_interval=health_poll_interval,
    )


def _live_redstone_containers() -> set[str]:
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=redstone-", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=15,
    )
    return {line for line in result.stdout.splitlines() if line}


@skip_no_docker
def test_full_lifecycle_against_real_docker(tmp_path):
    provider = DockerSandboxProvider()
    manager = _manager(provider)
    workspace = _workspace(tmp_path, "ws_docker_full")
    before = _live_redstone_containers()

    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    started = manager.start(runtime.id, PROJECT_ID, workspace)

    try:
        assert started.state is RuntimeState.RUNNING
        assert started.sandbox_id is not None
        assert manager.health_check(runtime.id, PROJECT_ID) is True

        # The runtime layer must not have weakened anything Phase 4 already
        # proved about the container itself: no host secret in its
        # environment, and outbound network still denied by default.
        env_probe = provider.exec_in(started.sandbox_id, ("env",), timeout=5)
        assert "GEMINI_API_KEY" not in env_probe.stdout
        assert "AI_API_KEY" not in env_probe.stdout

        network_probe = provider.exec_in(
            started.sandbox_id,
            ("wget", "-T", "3", "-q", "-O", "-", "http://example.com"),
            timeout=8,
        )
        assert network_probe.exit_code != 0   # NetworkPolicy.DENY still holds

        stopped = manager.stop(runtime.id, PROJECT_ID)
        assert stopped.state is RuntimeState.STOPPED
    finally:
        manager.destroy(runtime.id, PROJECT_ID)

    after = _live_redstone_containers()
    assert after == before   # zero orphaned containers


@skip_no_docker
def test_health_check_detects_a_real_container_crash(tmp_path):
    provider = DockerSandboxProvider()
    manager = _manager(provider)
    workspace = _workspace(tmp_path, "ws_docker_crash")

    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    started = manager.start(runtime.id, PROJECT_ID, workspace)

    try:
        assert manager.health_check(runtime.id, PROJECT_ID) is True

        # Simulate a real crash out-of-band from RuntimeManager -- e.g. the
        # dev server process inside the container segfaulting.
        provider.kill(started.sandbox_id)
        time.sleep(0.5)

        assert manager.health_check(runtime.id, PROJECT_ID) is False
        crashed = manager.get(runtime.id, PROJECT_ID)
        assert crashed.state is RuntimeState.FAILED
    finally:
        manager.destroy(runtime.id, PROJECT_ID)


@skip_no_docker
def test_restart_against_real_docker_replaces_the_container(tmp_path):
    provider = DockerSandboxProvider()
    manager = _manager(provider)
    workspace = _workspace(tmp_path, "ws_docker_restart")

    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    old = manager.start(runtime.id, PROJECT_ID, workspace)
    assert manager.health_check(runtime.id, PROJECT_ID) is True

    try:
        restarted = manager.restart(runtime.id, PROJECT_ID, workspace)

        assert restarted.id != runtime.id
        assert restarted.state is RuntimeState.RUNNING
        assert restarted.sandbox_id != old.sandbox_id
        assert manager.health_check(restarted.id, PROJECT_ID) is True

        live = _live_redstone_containers()
        assert old.sandbox_id not in live
        assert restarted.sandbox_id in live
    finally:
        manager.destroy(restarted.id, PROJECT_ID)


@skip_no_docker
def test_start_failure_leaves_no_orphaned_container(tmp_path):
    """A workspace with no package.json and no server.js: the fixed
    START_DEV_SERVER command (`npm run dev`) fails immediately since there is
    no `dev` script. The runtime must end up FAILED, not RUNNING, and the
    dead container must not be left behind."""
    provider = DockerSandboxProvider()
    manager = _manager(provider, max_startup_seconds=10.0, health_poll_interval=0.5)
    workspace = WorkspaceManager(tmp_path / "workspaces").create("ws_docker_no_project")
    before = _live_redstone_containers()

    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    with pytest.raises(RedstoneRuntimeError):
        manager.start(runtime.id, PROJECT_ID, workspace)

    assert manager.get(runtime.id, PROJECT_ID).state is RuntimeState.FAILED
    after = _live_redstone_containers()
    assert after == before
