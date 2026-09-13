"""Phase 5.1 RuntimeManager hardening.

UNIT / MOCKED (FakeSandboxProvider): error-code wiring, ownership labels,
orphan-reconciliation policy, operator configuration bounds.

REAL DOCKER (skipped with a reason when no daemon): orphan recovery across a
simulated Redstone process restart, against real containers, including a
malformed Redstone-labelled container and unrelated containers that must be
left alone.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from redstone.config import Limits
from redstone.domain.models import Framework, RuntimeState
from redstone.runtime.errors import RedstoneRuntimeError, RuntimeErrorCode
from redstone.runtime.manager import (
    PROJECT_LABEL,
    PURPOSE_LABEL,
    RUNTIME_LABEL,
    WORKSPACE_LABEL,
    RuntimeManager,
)
from redstone.sandbox.errors import RedstoneSandboxError, SandboxErrorCode
from redstone.sandbox.models import SandboxResult
from redstone.sandbox.providers.docker_provider import DockerSandboxProvider, docker_available
from redstone.workspace.manager import WorkspaceManager
from runtime_fakes import FakeSandboxProvider

PROJECT_ID = "prj_hardening"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "react_vite_ts_min"


def _workspace(tmp_path, workspace_id="ws_hardening", with_package_json=False):
    workspace = WorkspaceManager(tmp_path / "workspaces").create(workspace_id)
    if with_package_json:
        (workspace.project_root / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")
    return workspace


def _manager(provider, **kwargs):
    kwargs.setdefault("max_startup_seconds", 2.0)
    kwargs.setdefault("health_check_timeout", 0.5)
    kwargs.setdefault("health_poll_interval", 0.05)
    return RuntimeManager(provider, **kwargs)


def _result(**overrides):
    base = dict(exit_code=0, stdout="", stderr="", truncated=False, timed_out=False,
                duration_seconds=0.1)
    base.update(overrides)
    return SandboxResult(**base)


# ============================================================ error wiring

def test_install_killed_by_storage_quota_is_resource_limit(tmp_path):
    provider = FakeSandboxProvider(wait_result=_result(exit_code=137,
                                                       resource_limit_exceeded="storage_mb"))
    manager = _manager(provider)
    workspace = _workspace(tmp_path, with_package_json=True)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    with pytest.raises(RedstoneRuntimeError) as caught:
        manager.start(runtime.id, PROJECT_ID, workspace)

    assert caught.value.code is RuntimeErrorCode.RESOURCE_LIMIT
    assert "storage_mb" in caught.value.safe_message
    failed = manager.get(runtime.id, PROJECT_ID)
    assert failed.state is RuntimeState.FAILED
    assert failed.last_error["error_code"] == "RUNTIME_RESOURCE_LIMIT"


def test_install_timeout_is_timeout_not_start_failed(tmp_path):
    provider = FakeSandboxProvider(wait_result=_result(exit_code=None, timed_out=True))
    manager = _manager(provider)
    workspace = _workspace(tmp_path, with_package_json=True)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    with pytest.raises(RedstoneRuntimeError) as caught:
        manager.start(runtime.id, PROJECT_ID, workspace)
    assert caught.value.code is RuntimeErrorCode.TIMEOUT


def test_install_nonzero_exit_is_still_start_failed(tmp_path):
    provider = FakeSandboxProvider(install_ok=False)
    manager = _manager(provider)
    workspace = _workspace(tmp_path, with_package_json=True)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    with pytest.raises(RedstoneRuntimeError) as caught:
        manager.start(runtime.id, PROJECT_ID, workspace)
    assert caught.value.code is RuntimeErrorCode.START_FAILED


@pytest.mark.parametrize("sandbox_code, runtime_code", [
    (SandboxErrorCode.CREATE_FAILED, RuntimeErrorCode.CREATE_FAILED),
    (SandboxErrorCode.PROVIDER_UNAVAILABLE, RuntimeErrorCode.CREATE_FAILED),
    (SandboxErrorCode.TIMEOUT, RuntimeErrorCode.TIMEOUT),
    (SandboxErrorCode.START_FAILED, RuntimeErrorCode.START_FAILED),
])
def test_sandbox_errors_map_to_specific_runtime_codes(tmp_path, sandbox_code, runtime_code):
    provider = FakeSandboxProvider(create_error=RedstoneSandboxError(sandbox_code))
    manager = _manager(provider)
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    with pytest.raises(RedstoneRuntimeError) as caught:
        manager.start(runtime.id, PROJECT_ID, workspace)

    assert caught.value.code is runtime_code
    assert manager.get(runtime.id, PROJECT_ID).last_error["error_code"] == runtime_code.value
    # slot released: the workspace can start again
    assert manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS).id != runtime.id


def test_running_runtime_killed_by_quota_reports_resource_limit(tmp_path):
    provider = FakeSandboxProvider()
    manager = _manager(provider)
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    started = manager.start(runtime.id, PROJECT_ID, workspace)

    provider.exceed(started.sandbox_id, "storage_mb")

    assert manager.health_check(runtime.id, PROJECT_ID) is False
    crashed = manager.get(runtime.id, PROJECT_ID)
    assert crashed.state is RuntimeState.FAILED
    assert crashed.last_error["error_code"] == "RUNTIME_RESOURCE_LIMIT"


def test_ordinary_crash_is_still_healthcheck_failed(tmp_path):
    from redstone.sandbox.models import SandboxState
    provider = FakeSandboxProvider()
    manager = _manager(provider)
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    started = manager.start(runtime.id, PROJECT_ID, workspace)
    provider.force_state(started.sandbox_id, SandboxState.FAILED)

    manager.health_check(runtime.id, PROJECT_ID)
    assert manager.get(runtime.id, PROJECT_ID).last_error["error_code"] == "RUNTIME_HEALTHCHECK_FAILED"


def test_every_public_error_code_is_either_raised_or_documented_reserved():
    source = (Path(__file__).parents[2] / "src/redstone/runtime/manager.py").read_text("utf-8")
    source += (Path(__file__).parents[2] / "src/redstone/api/app.py").read_text("utf-8")
    source += (Path(__file__).parents[2] / "src/redstone/runtime/models.py").read_text("utf-8")
    docstring = RuntimeErrorCode.__doc__ or ""
    for code in RuntimeErrorCode:
        used = f"RuntimeErrorCode.{code.name}" in source
        reserved = f"{code.value}" in docstring or code.name in docstring
        assert used or reserved, code


# ============================================================ labels

def test_every_runtime_sandbox_carries_ownership_labels(tmp_path):
    provider = FakeSandboxProvider()
    manager = _manager(provider)
    workspace = _workspace(tmp_path, with_package_json=True)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    manager.start(runtime.id, PROJECT_ID, workspace)

    purposes = []
    for entry in provider.list_managed():
        labels = entry["labels"]
        assert labels[PROJECT_LABEL] == PROJECT_ID
        assert labels[WORKSPACE_LABEL] == workspace.id
        assert labels[RUNTIME_LABEL] == runtime.id
        purposes.append(labels[PURPOSE_LABEL])
    assert "start_dev_server" in purposes


# ==================================================== orphan reconciliation

def _labels(runtime_id, workspace_id="ws_old", project_id="prj_old"):
    return {PROJECT_LABEL: project_id, WORKSPACE_LABEL: workspace_id, RUNTIME_LABEL: runtime_id}


def test_reconcile_destroys_orphans_and_keeps_live_runtimes(tmp_path):
    provider = FakeSandboxProvider()
    manager = _manager(provider)
    workspace = _workspace(tmp_path)
    live = manager.start(manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS).id,
                         PROJECT_ID, workspace)

    provider.plant("redstone-orphan0001", _labels("rt_fromdeadprocess"))
    provider.plant("redstone-malformed01", _labels("../../etc"))
    provider.plant("redstone-nolabels001", {})
    provider.plant("somebody-elses-thing", _labels("rt_whatever"))

    report = manager.reconcile_orphaned_containers()

    assert set(report["destroyed"]) == {"redstone-orphan0001", "redstone-malformed01",
                                        "redstone-nolabels001"}
    assert report["malformed"] == 2
    assert report["kept"] == 1
    assert report["skipped_unrecognised"] == 1
    remaining = {e["sandbox_id"] for e in provider.list_managed()}
    assert live.sandbox_id in remaining
    assert "somebody-elses-thing" in remaining
    assert manager.health_check(live.id, PROJECT_ID) is True


def test_reconcile_in_a_fresh_process_destroys_everything_it_does_not_track(tmp_path):
    provider = FakeSandboxProvider()
    first = _manager(provider)
    workspace = _workspace(tmp_path)
    old = first.start(first.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS).id,
                      PROJECT_ID, workspace)

    second = _manager(provider)   # "the process restarted": empty memory, same backend
    report = second.reconcile_orphaned_containers()

    assert report["destroyed"] == [old.sandbox_id]
    assert provider.list_managed() == ()
    fresh = second.start(second.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS).id,
                         PROJECT_ID, workspace)
    assert [e["sandbox_id"] for e in provider.list_managed()] == [fresh.sandbox_id]


def test_reconcile_is_idempotent(tmp_path):
    provider = FakeSandboxProvider()
    manager = _manager(provider)
    provider.plant("redstone-orphan0002", _labels("rt_gone"))
    assert len(manager.reconcile_orphaned_containers()["destroyed"]) == 1
    assert manager.reconcile_orphaned_containers()["destroyed"] == []


# ========================================================= operator config

@pytest.mark.parametrize("var, raw, field, expected", [
    ("REDSTONE_SANDBOX_MEMORY_MB", "1024", "sandbox_memory_mb", 1024),
    ("REDSTONE_SANDBOX_MEMORY_MB", "0", "sandbox_memory_mb", 512),
    ("REDSTONE_SANDBOX_MEMORY_MB", "-1", "sandbox_memory_mb", 512),
    ("REDSTONE_SANDBOX_MEMORY_MB", "999999", "sandbox_memory_mb", 512),
    ("REDSTONE_SANDBOX_MEMORY_MB", "lots", "sandbox_memory_mb", 512),
    ("REDSTONE_SANDBOX_PIDS", "5", "sandbox_pids", 128),
    ("REDSTONE_SANDBOX_PIDS", "256", "sandbox_pids", 256),
    ("REDSTONE_SANDBOX_STORAGE_MB", "0", "sandbox_storage_mb", 1024),
    ("REDSTONE_SANDBOX_STORAGE_MB", "2048", "sandbox_storage_mb", 2048),
    ("REDSTONE_SANDBOX_STORAGE_MB", "1e9", "sandbox_storage_mb", 1024),
    ("REDSTONE_SANDBOX_CPU_CORES", "0.5", "sandbox_cpu_cores", 0.5),
    ("REDSTONE_SANDBOX_CPU_CORES", "nan", "sandbox_cpu_cores", 1.0),
    ("REDSTONE_SANDBOX_CPU_CORES", "inf", "sandbox_cpu_cores", 1.0),
    ("REDSTONE_SANDBOX_CPU_CORES", "0", "sandbox_cpu_cores", 1.0),
    ("REDSTONE_SANDBOX_OUTPUT_BYTES", "1", "sandbox_output_bytes", 256 * 1024),
])
def test_sandbox_limits_from_env_fail_closed(monkeypatch, var, raw, field, expected):
    monkeypatch.setenv(var, raw)
    assert getattr(Limits.from_env(), field) == expected


def test_no_environment_variable_selects_the_runtime_image():
    """The image is a reviewed constant, never operator/project/agent input."""
    source = (Path(__file__).parents[2] / "src/redstone/config.py").read_text("utf-8")
    assert "IMAGE" not in source.upper().replace("IMAGES", "")


# ================================================ REAL DOCKER: restart recovery

skip_no_docker = pytest.mark.skipif(not docker_available(), reason="Docker daemon not reachable")


def _docker(*args, check=False):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60, check=check)


def _exists(name):
    return _docker("inspect", name).returncode == 0


@skip_no_docker
def test_orphaned_runtime_is_recovered_after_a_simulated_process_restart(tmp_path):
    workspace = WorkspaceManager(tmp_path / "workspaces").create("ws_restart")
    for item in FIXTURE_DIR.iterdir():
        shutil.copy(item, workspace.project_root / item.name)

    suffix = uuid.uuid4().hex[:8]
    unrelated = f"rs-unrelated-{suffix}"
    impostor = f"notredstone-{suffix}"
    malformed = f"redstone-malformed{suffix}"
    _docker("run", "-d", "--name", unrelated, "alpine:3.19", "sleep", "300", check=True)
    _docker("run", "-d", "--name", impostor, "--label", "redstone.managed=true",
            "alpine:3.19", "sleep", "300", check=True)

    second = None
    fresh = None
    try:
        # ---- Process A: create and start a real runtime, then "die".
        first = RuntimeManager(DockerSandboxProvider(), max_startup_seconds=45.0)
        old = first.start(first.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS).id,
                          PROJECT_ID, workspace)
        assert _exists(old.sandbox_id)

        # A still-live process keeps its own runtime.
        assert first.reconcile_orphaned_containers()["kept"] >= 1
        assert _exists(old.sandbox_id)
        del first   # metadata lost; the container is still running

        # Planted only now: process A's own reconcile above would (correctly)
        # have destroyed it already.
        _docker("run", "-d", "--name", malformed, "--label", "redstone.managed=true",
                "--label", "redstone.runtime_id=../../bad", "alpine:3.19", "sleep", "300",
                check=True)

        # ---- Process B: fresh memory, same Docker daemon.
        second = RuntimeManager(DockerSandboxProvider(), max_startup_seconds=45.0)
        report = second.reconcile_orphaned_containers()

        assert old.sandbox_id in report["destroyed"]
        assert malformed in report["destroyed"]
        assert report["malformed"] >= 1
        assert not _exists(old.sandbox_id)
        assert not _exists(malformed)
        assert _exists(unrelated), "an unlabelled container must never be touched"
        assert _exists(impostor), "a labelled container without Redstone's name must be skipped"

        # No duplicate: exactly one runtime container for the workspace after restart.
        fresh = second.start(second.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS).id,
                             PROJECT_ID, workspace)
        owned = _docker("ps", "-a", "--filter", f"label={WORKSPACE_LABEL}={workspace.id}",
                        "--format", "{{.Names}}").stdout.split()
        assert owned == [fresh.sandbox_id]
    finally:
        if second is not None and fresh is not None:
            second.destroy(fresh.id, PROJECT_ID)
        for name in (unrelated, impostor, malformed):
            _docker("rm", "-f", name)
