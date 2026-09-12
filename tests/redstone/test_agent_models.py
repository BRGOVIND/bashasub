"""Agent domain model: state machine, immutability, safe serialisation."""

import pytest

from redstone.agent.errors import RedstoneAgentError
from redstone.agent.models import (
    AgentMessage,
    AgentStatus,
    AgentStep,
    AgentTask,
    StepType,
    TERMINAL_STATUSES,
    can_transition,
)


def _task():
    return AgentTask.create("prj_1", "ws_1", "do something")


# --------------------------------------------------------------- creation

def test_task_creation_pins_project_and_workspace():
    task = _task()

    assert task.project_id == "prj_1"
    assert task.workspace_id == "ws_1"
    assert task.status == AgentStatus.CREATED
    assert task.messages[0].role == "user"
    assert task.messages[0].content == "do something"


# -------------------------------------------------------------- transitions

@pytest.mark.parametrize(
    "start,target",
    [
        (AgentStatus.CREATED, AgentStatus.PLANNING),
        (AgentStatus.PLANNING, AgentStatus.INSPECTING),
        (AgentStatus.PLANNING, AgentStatus.EDITING),
        (AgentStatus.PLANNING, AgentStatus.VALIDATING),
        (AgentStatus.PLANNING, AgentStatus.COMPLETED),
        (AgentStatus.INSPECTING, AgentStatus.EDITING),
        (AgentStatus.EDITING, AgentStatus.VALIDATING),
        (AgentStatus.VALIDATING, AgentStatus.INSPECTING),
        (AgentStatus.VALIDATING, AgentStatus.COMPLETED),
    ],
)
def test_valid_transitions(start, target):
    assert can_transition(start, target)


@pytest.mark.parametrize(
    "start,target",
    [
        (AgentStatus.CREATED, AgentStatus.COMPLETED),   # must plan first
        (AgentStatus.CREATED, AgentStatus.EDITING),
        (AgentStatus.COMPLETED, AgentStatus.EDITING),    # terminal, no way out
        (AgentStatus.FAILED, AgentStatus.PLANNING),
        (AgentStatus.CANCELLED, AgentStatus.COMPLETED),
        (AgentStatus.TIMED_OUT, AgentStatus.INSPECTING),
    ],
)
def test_invalid_transitions(start, target):
    assert not can_transition(start, target)


@pytest.mark.parametrize(
    "start,target",
    [
        (AgentStatus.PLANNING, AgentStatus.FAILED),
        (AgentStatus.INSPECTING, AgentStatus.CANCELLED),
        (AgentStatus.EDITING, AgentStatus.TIMED_OUT),
        (AgentStatus.VALIDATING, AgentStatus.FAILED),
    ],
)
def test_any_active_state_can_fail_cancel_or_timeout(start, target):
    assert can_transition(start, target)


def test_terminal_states_accept_no_further_transition():
    for status in TERMINAL_STATUSES:
        for target in AgentStatus:
            assert not can_transition(status, target)


def test_with_status_enforces_the_state_machine():
    task = _task()
    with pytest.raises(RedstoneAgentError):
        task.with_status(AgentStatus.COMPLETED)   # CREATED -> COMPLETED is illegal

    advanced = task.with_status(AgentStatus.PLANNING)
    assert advanced.status == AgentStatus.PLANNING
    assert task.status == AgentStatus.CREATED   # original is untouched


def test_cannot_leave_a_terminal_task():
    task = _task().with_status(AgentStatus.PLANNING).with_status(AgentStatus.COMPLETED)

    with pytest.raises(RedstoneAgentError):
        task.with_status(AgentStatus.EDITING)


# --------------------------------------------------------------- immutability

def test_task_is_immutable():
    task = _task()
    with pytest.raises(Exception):
        task.status = AgentStatus.COMPLETED


def test_with_message_appends_without_mutating_original():
    task = _task()
    updated = task.with_message(AgentMessage(role="assistant", content="hi"))

    assert len(task.messages) == 1
    assert len(updated.messages) == 2


def test_with_step_and_iteration_accumulate():
    task = _task()
    step = AgentStep.create(StepType.TOOL_CALL, {"tool": "list_files"})
    task = task.with_step(step).with_iteration().with_iteration()

    assert len(task.steps) == 1
    assert task.iterations_used == 2


# -------------------------------------------------------------- serialisation

def test_to_dict_is_safe_and_summarised():
    task = _task().with_status(AgentStatus.PLANNING)
    data = task.to_dict()

    assert data["task_id"] == task.id
    assert data["status"] == "planning"
    assert "messages" not in data     # conversation content is not in the summary
    assert "steps" not in data


def test_to_dict_reports_current_step():
    task = _task().with_status(AgentStatus.PLANNING).with_step(
        AgentStep.create(StepType.TOOL_CALL, {"tool": "list_files"})
    )
    assert task.to_dict()["current_step"] == "tool_call"


def test_is_terminal_property():
    task = _task()
    assert not task.is_terminal
    assert task.with_status(AgentStatus.PLANNING).with_status(AgentStatus.FAILED).is_terminal
