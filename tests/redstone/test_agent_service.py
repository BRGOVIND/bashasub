"""AgentService: project registry, task lifecycle, concurrency, cancellation."""

from __future__ import annotations

import time

import pytest

from agent_fakes import FailingAIGateway, FakeAIGateway, action
from redstone.agent.errors import AgentErrorCode, RedstoneAgentError
from redstone.agent.models import AgentStatus
from redstone.agent.service import AgentService
from redstone.ai.errors import AIErrorCode
from redstone.config import AIConfig, Limits, RedstoneConfig
from redstone.domain.models import Framework


def _config(tmp_path, **limit_overrides):
    return RedstoneConfig(
        workspaces_root=tmp_path / "workspaces",
        limits=Limits(max_agent_iterations=10, **limit_overrides),
        ai=AIConfig(),
    )


def _service(tmp_path, gateway=None, **limit_overrides):
    return AgentService(_config(tmp_path, **limit_overrides), gateway=gateway or FakeAIGateway())


# --------------------------------------------------------------- projects

def test_create_project_provisions_a_real_workspace(tmp_path):
    service = _service(tmp_path)
    project = service.create_project("Demo")

    assert project.status.value == "ready"
    assert project.framework == Framework.STATIC
    workspace_dir = tmp_path / "workspaces" / project.workspace_id / "project"
    assert workspace_dir.is_dir()


def test_get_project_roundtrips(tmp_path):
    service = _service(tmp_path)
    created = service.create_project("Demo")

    assert service.get_project(created.id).id == created.id


def test_get_missing_project_is_a_safe_error(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(RedstoneAgentError) as caught:
        service.get_project("prj_missing")
    assert caught.value.code is AgentErrorCode.PROJECT_NOT_FOUND


def test_create_project_rejects_empty_name(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(RedstoneAgentError) as caught:
        service.create_project("   ")
    assert caught.value.code is AgentErrorCode.INVALID_REQUEST


def test_each_project_gets_its_own_isolated_workspace(tmp_path):
    service = _service(tmp_path)
    a = service.create_project("A")
    b = service.create_project("B")

    assert a.workspace_id != b.workspace_id


# ------------------------------------------------------------------- tasks

def test_start_task_runs_synchronously_and_returns_final_state(tmp_path):
    gateway = FakeAIGateway([action("complete", summary="done")])
    service = _service(tmp_path, gateway=gateway)
    project = service.create_project("Demo")

    task = service.start_task(project.id, "do something")

    assert task.status == AgentStatus.COMPLETED
    assert task.project_id == project.id


def test_start_task_rejects_empty_message(tmp_path):
    service = _service(tmp_path)
    project = service.create_project("Demo")

    with pytest.raises(RedstoneAgentError) as caught:
        service.start_task(project.id, "")
    assert caught.value.code is AgentErrorCode.INVALID_REQUEST


def test_start_task_on_missing_project_is_safe(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(RedstoneAgentError) as caught:
        service.start_task("prj_missing", "do something")
    assert caught.value.code is AgentErrorCode.PROJECT_NOT_FOUND


def test_get_task_roundtrips(tmp_path):
    gateway = FakeAIGateway([action("complete", summary="done")])
    service = _service(tmp_path, gateway=gateway)
    project = service.create_project("Demo")
    task = service.start_task(project.id, "do something")

    assert service.get_task(task.id).id == task.id


def test_get_missing_task_is_a_safe_error(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(RedstoneAgentError) as caught:
        service.get_task("task_missing")
    assert caught.value.code is AgentErrorCode.NOT_FOUND


def test_task_events_are_recorded_and_retrievable(tmp_path):
    gateway = FakeAIGateway([action("complete", summary="done")])
    service = _service(tmp_path, gateway=gateway)
    project = service.create_project("Demo")
    task = service.start_task(project.id, "do something")

    events = service.get_task_events(task.id)

    assert any(e["type"] == "agent.started" for e in events)
    assert any(e["type"] == "agent.completed" for e in events)


def test_events_for_missing_task_is_a_safe_error(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(RedstoneAgentError) as caught:
        service.get_task_events("task_missing")
    assert caught.value.code is AgentErrorCode.NOT_FOUND


def test_ai_failure_produces_a_failed_task_not_an_exception(tmp_path):
    service = _service(tmp_path, gateway=FailingAIGateway(AIErrorCode.PROVIDER_UNAVAILABLE))
    project = service.create_project("Demo")

    task = service.start_task(project.id, "do something")

    assert task.status == AgentStatus.FAILED


# ------------------------------------------------------------ workspace busy

def test_workspace_busy_is_returned_not_blocked(tmp_path):
    """A second task on the same project's workspace fails immediately while
    the first is still running -- it must never queue/block."""
    gateway = FakeAIGateway(delay=0.4)
    service = _service(tmp_path, gateway=gateway)
    project = service.create_project("Demo")

    first = service.start_task(project.id, "first", background=True)
    time.sleep(0.05)

    started_at = time.monotonic()
    with pytest.raises(RedstoneAgentError) as caught:
        service.start_task(project.id, "second")
    elapsed = time.monotonic() - started_at

    assert caught.value.code is AgentErrorCode.WORKSPACE_BUSY
    assert elapsed < 0.2   # rejected immediately, not queued behind the first task

    _poll_until_terminal(service, first.id)   # let the background task finish before the next test


def test_workspace_is_free_again_after_the_task_completes(tmp_path):
    """A second task on the same project, after the first finished, must
    succeed rather than incorrectly reporting WORKSPACE_BUSY forever."""
    gateway = FakeAIGateway([
        action("complete", summary="done"),
        action("complete", summary="done again"),
    ])
    service = _service(tmp_path, gateway=gateway)
    project = service.create_project("Demo")

    first = service.start_task(project.id, "first")
    assert first.status == AgentStatus.COMPLETED

    second = service.start_task(project.id, "second")
    assert second.status == AgentStatus.COMPLETED
    assert second.id != first.id


def test_two_different_projects_are_never_mutually_busy(tmp_path):
    """Two projects on the SAME service (same admission-control registry)
    must have independent leases -- a slow task on one must not block the
    other. Both share one gateway; with no script it falls back to an
    immediate "complete" action, so project B's task returns right away."""
    service = AgentService(_config(tmp_path), gateway=FakeAIGateway(delay=0.3))
    a = service.create_project("A")
    b = service.create_project("B")

    first = service.start_task(a.id, "first", background=True)
    time.sleep(0.05)

    task_b = service.start_task(b.id, "unrelated")

    assert task_b.status == AgentStatus.COMPLETED
    _poll_until_terminal(service, first.id)


# ------------------------------------------------------------------ cancellation

def test_cancel_a_running_background_task(tmp_path):
    gateway = FakeAIGateway(default=action("tool_call", tool="list_files", arguments={}),
                            delay=0.15)
    service = _service(tmp_path, gateway=gateway)
    project = service.create_project("Demo")

    task = service.start_task(project.id, "loop forever", background=True)
    time.sleep(0.2)
    service.cancel_task(task.id)

    final = _poll_until_terminal(service, task.id)
    assert final.status == AgentStatus.CANCELLED
    assert final.iterations_used < 10   # stopped well before the iteration limit


def test_cancel_missing_task_is_a_safe_error(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(RedstoneAgentError) as caught:
        service.cancel_task("task_missing")
    assert caught.value.code is AgentErrorCode.NOT_FOUND


def test_cancelling_an_already_completed_task_is_a_harmless_no_op(tmp_path):
    gateway = FakeAIGateway([action("complete", summary="done")])
    service = _service(tmp_path, gateway=gateway)
    project = service.create_project("Demo")
    task = service.start_task(project.id, "do something")

    service.cancel_task(task.id)   # must not raise

    assert service.get_task(task.id).status == AgentStatus.COMPLETED


def _poll_until_terminal(service, task_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = service.get_task(task_id)
        if task.is_terminal:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} never reached a terminal state")


# -------------------------------------------------------- snapshots/changesets

def test_completed_task_with_writes_has_a_snapshot_and_changeset(tmp_path):
    gateway = FakeAIGateway([
        action("tool_call", tool="write_file", arguments={"path": "a.txt", "content": "x"}),
        action("complete", summary="wrote a.txt"),
    ])
    service = _service(tmp_path, gateway=gateway)
    project = service.create_project("Demo")

    task = service.start_task(project.id, "write a file")

    assert task.snapshot_id is not None
    assert task.changeset_id is not None


def test_read_only_task_has_no_snapshot(tmp_path):
    gateway = FakeAIGateway([
        action("tool_call", tool="list_files", arguments={}),
        action("complete", summary="looked around"),
    ])
    service = _service(tmp_path, gateway=gateway)
    project = service.create_project("Demo")

    task = service.start_task(project.id, "look around")

    assert task.snapshot_id is None
    assert task.changeset_id is not None   # a changeset is always produced (even if empty)
