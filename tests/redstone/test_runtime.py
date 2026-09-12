"""RuntimeManager: lifecycle, ownership, idempotency, crash detection and
concurrency-race safety, against a fake in-memory SandboxProvider.

Real sandbox isolation (Docker) is verified separately in test_sandbox.py and
test_runtime_docker_integration.py -- this file is about RuntimeManager's own
orchestration logic, which is why it mocks the provider layer (the one layer
the Phase 4/5 testing policy explicitly allows mocking).
"""

from __future__ import annotations

import threading
import time

import pytest

from redstone.domain.models import Framework, RuntimeState
from redstone.runtime.errors import RedstoneRuntimeError, RuntimeErrorCode
from redstone.runtime.manager import RuntimeManager
from redstone.sandbox.models import SandboxState
from redstone.workspace.manager import WorkspaceManager
from runtime_fakes import FakeSandboxProvider

PROJECT_ID = "prj_demo"


def _workspace(tmp_path, workspace_id="ws_demo", with_package_json=False):
    workspace = WorkspaceManager(tmp_path / "workspaces").create(workspace_id)
    if with_package_json:
        (workspace.project_root / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
    return workspace


def _manager(provider=None, **kwargs):
    kwargs.setdefault("max_startup_seconds", 2.0)
    kwargs.setdefault("health_check_timeout", 0.5)
    kwargs.setdefault("stop_grace_seconds", 1.0)
    kwargs.setdefault("health_poll_interval", 0.05)
    return RuntimeManager(provider or FakeSandboxProvider(), **kwargs)


# ------------------------------------------------------------------- create

def test_create_returns_a_created_runtime(tmp_path):
    manager = _manager()
    workspace = _workspace(tmp_path)

    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    assert runtime.state is RuntimeState.CREATED
    assert runtime.project_id == PROJECT_ID
    assert runtime.workspace_id == workspace.id


def test_create_rejects_a_second_runtime_for_the_same_workspace(tmp_path):
    manager = _manager()
    workspace = _workspace(tmp_path)
    manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    with pytest.raises(RedstoneRuntimeError) as caught:
        manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    assert caught.value.code is RuntimeErrorCode.BUSY


def test_create_for_a_different_workspace_succeeds(tmp_path):
    manager = _manager()
    ws_a = _workspace(tmp_path, "ws_a")
    ws_b = _workspace(tmp_path, "ws_b")

    manager.create(PROJECT_ID, ws_a, Framework.REACT_VITE_TS)
    second = manager.create(PROJECT_ID, ws_b, Framework.REACT_VITE_TS)

    assert second.workspace_id == ws_b.id


# -------------------------------------------------------------------- start

def test_start_reaches_running_once_health_check_succeeds(tmp_path):
    provider = FakeSandboxProvider(health_check_failures=1)
    manager = _manager(provider)
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    started = manager.start(runtime.id, PROJECT_ID, workspace)

    assert started.state is RuntimeState.RUNNING
    assert started.sandbox_id is not None
    assert not provider.wait_calls   # no package.json -> no install phase


def test_start_runs_install_first_when_package_json_present(tmp_path):
    provider = FakeSandboxProvider()
    manager = _manager(provider)
    workspace = _workspace(tmp_path, with_package_json=True)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    started = manager.start(runtime.id, PROJECT_ID, workspace)

    assert started.state is RuntimeState.RUNNING
    assert len(provider.wait_calls) == 1               # the install sandbox was waited on
    assert provider.wait_calls[0] in provider.destroy_calls  # and torn down afterward


def test_start_is_idempotent_when_already_running(tmp_path):
    manager = _manager()
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    first = manager.start(runtime.id, PROJECT_ID, workspace)

    second = manager.start(runtime.id, PROJECT_ID, workspace)

    assert second.state is RuntimeState.RUNNING
    assert second.sandbox_id == first.sandbox_id       # no second sandbox was created


def test_start_fails_the_runtime_when_install_fails(tmp_path):
    provider = FakeSandboxProvider(install_ok=False)
    manager = _manager(provider)
    workspace = _workspace(tmp_path, with_package_json=True)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    with pytest.raises(RedstoneRuntimeError) as caught:
        manager.start(runtime.id, PROJECT_ID, workspace)

    assert caught.value.code is RuntimeErrorCode.START_FAILED
    failed = manager.get(runtime.id, PROJECT_ID)
    assert failed.state is RuntimeState.FAILED
    assert failed.last_error["error_code"] == RuntimeErrorCode.START_FAILED.value

    # The workspace slot must be released so the caller can try again.
    retry = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    assert retry.id != runtime.id


def test_start_fails_when_the_dev_server_never_becomes_healthy(tmp_path):
    provider = FakeSandboxProvider(never_healthy=True)
    manager = _manager(provider, max_startup_seconds=0.2, health_poll_interval=0.05)
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    with pytest.raises(RedstoneRuntimeError) as caught:
        manager.start(runtime.id, PROJECT_ID, workspace)

    assert caught.value.code is RuntimeErrorCode.HEALTHCHECK_FAILED
    failed = manager.get(runtime.id, PROJECT_ID)
    assert failed.state is RuntimeState.FAILED
    # The never-healthy sandbox must have been torn down, not left running.
    assert failed.sandbox_id not in provider.live_sandbox_ids()


def test_start_never_marks_running_while_the_sandbox_has_crashed(tmp_path):
    """A sandbox that dies mid-startup (before any health probe succeeds)
    must never be reported as RUNNING merely because it was launched."""
    provider = FakeSandboxProvider(never_healthy=True)
    manager = _manager(provider, max_startup_seconds=5.0, health_poll_interval=0.02)
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    def _crash_soon():
        time.sleep(0.05)
        for sandbox_id in provider.live_sandbox_ids():
            provider.force_state(sandbox_id, SandboxState.FAILED)

    threading.Thread(target=_crash_soon, daemon=True).start()

    with pytest.raises(RedstoneRuntimeError) as caught:
        manager.start(runtime.id, PROJECT_ID, workspace)
    assert caught.value.code is RuntimeErrorCode.HEALTHCHECK_FAILED
    assert manager.get(runtime.id, PROJECT_ID).state is RuntimeState.FAILED


def test_start_of_unknown_runtime_raises_not_found(tmp_path):
    manager = _manager()
    workspace = _workspace(tmp_path)

    with pytest.raises(RedstoneRuntimeError) as caught:
        manager.start("rt_missing", PROJECT_ID, workspace)
    assert caught.value.code is RuntimeErrorCode.NOT_FOUND


def test_operations_reject_the_wrong_project_id(tmp_path):
    manager = _manager()
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    with pytest.raises(RedstoneRuntimeError) as caught:
        manager.start(runtime.id, "prj_other", workspace)
    assert caught.value.code is RuntimeErrorCode.NOT_OWNED


# --------------------------------------------------------------------- stop

def test_stop_transitions_to_stopped_and_frees_the_slot(tmp_path):
    manager = _manager()
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    manager.start(runtime.id, PROJECT_ID, workspace)

    stopped = manager.stop(runtime.id, PROJECT_ID)
    assert stopped.state is RuntimeState.STOPPED

    # Slot freed -> a new runtime can be created for the same workspace.
    again = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    assert again.id != runtime.id


def test_stop_is_idempotent(tmp_path):
    manager = _manager()
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    manager.start(runtime.id, PROJECT_ID, workspace)
    manager.stop(runtime.id, PROJECT_ID)

    twice = manager.stop(runtime.id, PROJECT_ID)   # must not raise
    assert twice.state is RuntimeState.STOPPED


def test_stop_of_a_never_started_runtime_is_safe(tmp_path):
    manager = _manager()
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    stopped = manager.stop(runtime.id, PROJECT_ID)
    assert stopped.state is RuntimeState.STOPPED


# --------------------------------------------------------------------- kill

def test_kill_is_idempotent(tmp_path):
    manager = _manager()
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    manager.start(runtime.id, PROJECT_ID, workspace)

    manager.kill(runtime.id, PROJECT_ID)
    twice = manager.kill(runtime.id, PROJECT_ID)
    assert twice.state is RuntimeState.KILLED


# ------------------------------------------------------------------ destroy

def test_destroy_of_unknown_id_does_not_raise(tmp_path):
    manager = _manager()
    manager.destroy("rt_missing")   # must not raise


def test_destroy_is_idempotent(tmp_path):
    manager = _manager()
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    manager.start(runtime.id, PROJECT_ID, workspace)

    manager.destroy(runtime.id, PROJECT_ID)
    manager.destroy(runtime.id, PROJECT_ID)   # second call must not raise

    # destroy() tombstones the record (state=DESTROYED) rather than forgetting
    # it -- this is what lets restart() report "historical generations".
    assert manager.get(runtime.id, PROJECT_ID).state is RuntimeState.DESTROYED


def test_destroy_removes_the_underlying_sandbox(tmp_path):
    provider = FakeSandboxProvider()
    manager = _manager(provider)
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    started = manager.start(runtime.id, PROJECT_ID, workspace)

    manager.destroy(runtime.id, PROJECT_ID)

    assert started.sandbox_id not in provider.live_sandbox_ids()


def test_destroy_rejects_the_wrong_project_id(tmp_path):
    manager = _manager()
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    with pytest.raises(RedstoneRuntimeError) as caught:
        manager.destroy(runtime.id, "prj_other")
    assert caught.value.code is RuntimeErrorCode.NOT_OWNED


def test_destroy_frees_the_workspace_slot(tmp_path):
    manager = _manager()
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

    manager.destroy(runtime.id, PROJECT_ID)

    again = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    assert again.id != runtime.id


# --------------------------------------------------------------- ownership

def test_get_missing_runtime_raises_not_found(tmp_path):
    manager = _manager()
    with pytest.raises(RedstoneRuntimeError) as caught:
        manager.get("rt_missing", PROJECT_ID)
    assert caught.value.code is RuntimeErrorCode.NOT_FOUND


def test_list_for_project_only_returns_that_projects_runtimes(tmp_path):
    manager = _manager()
    ws_a = _workspace(tmp_path, "ws_a")
    ws_b = _workspace(tmp_path, "ws_b")
    manager.create("prj_a", ws_a, Framework.REACT_VITE_TS)
    manager.create("prj_b", ws_b, Framework.REACT_VITE_TS)

    assert len(manager.list_for_project("prj_a")) == 1
    assert len(manager.list_for_project("prj_b")) == 1


# ---------------------------------------------------------------- crashes

def test_health_check_detects_a_crashed_sandbox(tmp_path):
    provider = FakeSandboxProvider()
    manager = _manager(provider)
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    started = manager.start(runtime.id, PROJECT_ID, workspace)

    provider.force_state(started.sandbox_id, SandboxState.FAILED)

    assert manager.health_check(runtime.id, PROJECT_ID) is False
    crashed = manager.get(runtime.id, PROJECT_ID)
    assert crashed.state is RuntimeState.FAILED
    assert crashed.last_error["error_code"] == RuntimeErrorCode.HEALTHCHECK_FAILED.value


def test_health_check_true_while_the_server_is_responding(tmp_path):
    manager = _manager()
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    manager.start(runtime.id, PROJECT_ID, workspace)

    assert manager.health_check(runtime.id, PROJECT_ID) is True


def test_reconcile_marks_orphaned_crashes_as_failed(tmp_path):
    provider = FakeSandboxProvider()
    manager = _manager(provider)
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    started = manager.start(runtime.id, PROJECT_ID, workspace)
    provider.force_state(started.sandbox_id, SandboxState.FAILED)

    corrected = manager.reconcile()

    assert corrected == 1
    assert manager.get(runtime.id, PROJECT_ID).state is RuntimeState.FAILED


def test_reconcile_is_a_noop_when_everything_is_healthy(tmp_path):
    manager = _manager()
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    manager.start(runtime.id, PROJECT_ID, workspace)

    assert manager.reconcile() == 0


# --------------------------------------------------------------------- restart

def test_restart_produces_a_new_runtime_generation(tmp_path):
    provider = FakeSandboxProvider()
    manager = _manager(provider)
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    manager.start(runtime.id, PROJECT_ID, workspace)

    restarted = manager.restart(runtime.id, PROJECT_ID, workspace)

    assert restarted.id != runtime.id
    assert restarted.state is RuntimeState.RUNNING
    # The old generation is tombstoned, not forgotten.
    assert manager.get(runtime.id, PROJECT_ID).state is RuntimeState.DESTROYED


def test_restart_leaves_no_orphaned_sandbox_from_the_old_generation(tmp_path):
    provider = FakeSandboxProvider()
    manager = _manager(provider)
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    old = manager.start(runtime.id, PROJECT_ID, workspace)

    manager.restart(runtime.id, PROJECT_ID, workspace)

    assert old.sandbox_id not in provider.live_sandbox_ids()


def test_restart_of_a_failed_runtime_recovers_cleanly(tmp_path):
    provider = FakeSandboxProvider(never_healthy=True)
    manager = _manager(provider, max_startup_seconds=0.2, health_poll_interval=0.02)
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    with pytest.raises(RedstoneRuntimeError):
        manager.start(runtime.id, PROJECT_ID, workspace)
    assert manager.get(runtime.id, PROJECT_ID).state is RuntimeState.FAILED

    provider.never_healthy = False   # simulate the underlying problem being fixed
    recovered = manager.restart(runtime.id, PROJECT_ID, workspace)

    assert recovered.state is RuntimeState.RUNNING
    assert recovered.id != runtime.id


# ----------------------------------------------------------- concurrency races

def test_simultaneous_creates_for_the_same_workspace_only_one_wins(tmp_path):
    manager = _manager(FakeSandboxProvider(create_delay=0.05))
    workspace = _workspace(tmp_path)
    results = []
    errors = []

    def _try_create():
        try:
            results.append(manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS))
        except RedstoneRuntimeError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_try_create) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 1
    assert len(errors) == 7
    assert all(e.code is RuntimeErrorCode.BUSY for e in errors)


def test_start_and_stop_race_leaves_a_consistent_terminal_state(tmp_path):
    for _ in range(10):
        provider = FakeSandboxProvider(start_delay=0.02)
        manager = _manager(provider)
        workspace = _workspace(tmp_path, workspace_id=f"ws_race_{_}")
        runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)

        outcomes = []

        def _start():
            try:
                outcomes.append(("start", manager.start(runtime.id, PROJECT_ID, workspace).state))
            except RedstoneRuntimeError as exc:
                outcomes.append(("start", exc.code))

        def _stop():
            time.sleep(0.01)
            outcomes.append(("stop", manager.stop(runtime.id, PROJECT_ID).state))

        t1, t2 = threading.Thread(target=_start), threading.Thread(target=_stop)
        t1.start(); t2.start()
        t1.join(); t2.join()

        final = manager.get(runtime.id, PROJECT_ID)
        assert final.state in (RuntimeState.RUNNING, RuntimeState.STOPPED)
        # Neither operation raised anything other than an expected runtime error.
        for _kind, outcome in outcomes:
            assert outcome in (RuntimeState.RUNNING, RuntimeState.STOPPED) or hasattr(outcome, "value")


def test_simultaneous_destroys_are_all_safe(tmp_path):
    provider = FakeSandboxProvider()
    manager = _manager(provider)
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    manager.start(runtime.id, PROJECT_ID, workspace)

    errors = []

    def _destroy():
        try:
            manager.destroy(runtime.id, PROJECT_ID)
        except Exception as exc:   # noqa: BLE001 -- asserting NOTHING escapes
            errors.append(exc)

    threads = [threading.Thread(target=_destroy) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert manager.get(runtime.id, PROJECT_ID).state is RuntimeState.DESTROYED


def test_restart_and_destroy_race_never_leaves_two_active_generations(tmp_path):
    provider = FakeSandboxProvider()
    manager = _manager(provider)
    workspace = _workspace(tmp_path)
    runtime = manager.create(PROJECT_ID, workspace, Framework.REACT_VITE_TS)
    manager.start(runtime.id, PROJECT_ID, workspace)

    outcomes = []

    def _restart():
        try:
            outcomes.append(manager.restart(runtime.id, PROJECT_ID, workspace))
        except RedstoneRuntimeError as exc:
            outcomes.append(exc)

    def _destroy():
        manager.destroy(runtime.id, PROJECT_ID)

    t1, t2 = threading.Thread(target=_restart), threading.Thread(target=_destroy)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Whatever happened, at most one runtime may hold the workspace's active slot.
    live = [r for r in manager.list_for_project(PROJECT_ID)
            if r.state not in (RuntimeState.DESTROYED,)]
    running_or_creating = [r for r in live if r.state in (
        RuntimeState.CREATED, RuntimeState.STARTING, RuntimeState.RUNNING, RuntimeState.IDLE,
    )]
    assert len(running_or_creating) <= 1
