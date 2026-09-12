"""The minimal Redstone agent API, over FastAPI's TestClient.

No real AI provider anywhere: the service injected into the app is built on
FakeAIGateway. No network call happens in this file.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_fakes import FakeAIGateway, action
from redstone.agent.service import AgentService
from redstone.api.app import create_app
from redstone.config import AIConfig, Limits, RedstoneConfig


def _client(tmp_path, gateway=None, **limit_overrides):
    config = RedstoneConfig(
        workspaces_root=tmp_path / "workspaces",
        limits=Limits(max_agent_iterations=10, **limit_overrides),
        ai=AIConfig(),
    )
    service = AgentService(config, gateway=gateway or FakeAIGateway([action("complete", summary="done")]))
    return TestClient(create_app(service=service))


def test_health(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/health")
    assert response.status_code == 200


def test_create_project(tmp_path):
    client = _client(tmp_path)

    response = client.post("/api/projects", json={"name": "My Project"})

    assert response.status_code == 200
    body = response.json()
    assert body["project_id"].startswith("prj_")
    assert body["workspace_id"].startswith("ws_")
    assert body["status"] == "ready"


def test_create_project_rejects_empty_name(tmp_path):
    client = _client(tmp_path)
    response = client.post("/api/projects", json={"name": ""})
    assert response.status_code == 422   # pydantic min_length validation


def test_start_agent_task(tmp_path):
    gateway = FakeAIGateway([
        action("tool_call", tool="write_file", arguments={"path": "a.txt", "content": "hi"}),
        action("complete", summary="wrote a.txt"),
    ])
    client = _client(tmp_path, gateway=gateway)
    project = client.post("/api/projects", json={"name": "Demo"}).json()

    response = client.post(f"/api/projects/{project['project_id']}/agent",
                           json={"message": "write a file"})

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"].startswith("task_")
    assert body["status"] == "completed"


def test_start_agent_task_does_not_accept_a_workspace_id(tmp_path):
    """The request schema has no field for it at all: a caller cannot even
    attempt to name a workspace directly, regardless of what value is sent."""
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Demo"}).json()

    response = client.post(
        f"/api/projects/{project['project_id']}/agent",
        json={"message": "do it", "workspace_id": "ws_someone_elses_workspace"},
    )

    # Extra field is silently ignored by pydantic (not an error), and the
    # server derives the workspace from the project regardless.
    assert response.status_code == 200
    task = client.get(f"/api/agent/tasks/{response.json()['task_id']}").json()
    assert task["project_id"] == project["project_id"]


def test_start_agent_task_missing_message(tmp_path):
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Demo"}).json()

    response = client.post(f"/api/projects/{project['project_id']}/agent", json={})

    assert response.status_code == 422


def test_start_agent_task_on_missing_project(tmp_path):
    client = _client(tmp_path)

    response = client.post("/api/projects/prj_missing/agent", json={"message": "x"})

    assert response.status_code == 404
    assert response.json()["error_code"] == "AGENT_PROJECT_NOT_FOUND"


def test_get_task(tmp_path):
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Demo"}).json()
    task = client.post(f"/api/projects/{project['project_id']}/agent",
                       json={"message": "x"}).json()

    response = client.get(f"/api/agent/tasks/{task['task_id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == task["task_id"]
    assert body["status"] == "completed"


def test_get_missing_task(tmp_path):
    client = _client(tmp_path)

    response = client.get("/api/agent/tasks/task_missing")

    assert response.status_code == 404
    assert response.json()["error_code"] == "AGENT_TASK_NOT_FOUND"


def test_get_task_events(tmp_path):
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Demo"}).json()
    task = client.post(f"/api/projects/{project['project_id']}/agent",
                       json={"message": "x"}).json()

    response = client.get(f"/api/agent/tasks/{task['task_id']}/events")

    assert response.status_code == 200
    events = response.json()["events"]
    assert any(e["type"] == "agent.started" for e in events)
    assert any(e["type"] == "agent.completed" for e in events)


def test_workspace_busy_returns_409(tmp_path):
    import threading
    import time

    gateway = FakeAIGateway(delay=0.3)
    client = _client(tmp_path, gateway=gateway)
    project = client.post("/api/projects", json={"name": "Demo"}).json()

    results = {}

    def _first():
        results["first"] = client.post(
            f"/api/projects/{project['project_id']}/agent", json={"message": "first"}
        )

    thread = threading.Thread(target=_first)
    thread.start()
    time.sleep(0.05)

    second = client.post(f"/api/projects/{project['project_id']}/agent", json={"message": "second"})

    assert second.status_code == 409
    assert second.json()["error_code"] == "AGENT_WORKSPACE_BUSY"

    thread.join(timeout=5)


def test_no_endpoint_exposes_ai_credentials(tmp_path):
    """Defensive sweep: whatever the API returns, it never contains anything
    that looks like a configured provider credential field."""
    client = _client(tmp_path)
    project_response = client.post("/api/projects", json={"name": "Demo"})
    task_response = client.post(
        f"/api/projects/{project_response.json()['project_id']}/agent",
        json={"message": "x"},
    )
    events_response = client.get(
        f"/api/agent/tasks/{task_response.json()['task_id']}/events"
    )

    for response in (project_response, task_response, events_response):
        body = response.text.lower()
        assert "api_key" not in body
        assert "authorization" not in body
        assert "bearer" not in body
