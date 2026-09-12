"""The minimal Redstone runtime API, over FastAPI's TestClient.

A FakeSandboxProvider backs every RuntimeManager here -- no Docker, no real
process, per the same policy test_agent_api.py already follows for the AI
provider (mock only at the outermost provider seam; real Docker behavior is
covered separately in test_runtime_docker_integration.py).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_fakes import FakeAIGateway, action
from redstone.agent.service import AgentService
from redstone.api.app import create_app
from redstone.config import AIConfig, Limits, RedstoneConfig
from redstone.runtime.manager import RuntimeManager
from runtime_fakes import FakeSandboxProvider


def _client(tmp_path, *, sandbox_provider=None, **manager_kwargs):
    config = RedstoneConfig(
        workspaces_root=tmp_path / "workspaces",
        limits=Limits(),
        ai=AIConfig(),
    )
    service = AgentService(config, gateway=FakeAIGateway([action("complete", summary="done")]))
    manager_kwargs.setdefault("max_startup_seconds", 2.0)
    manager_kwargs.setdefault("health_check_timeout", 0.5)
    manager_kwargs.setdefault("health_poll_interval", 0.05)
    manager = RuntimeManager(sandbox_provider or FakeSandboxProvider(), **manager_kwargs)
    return TestClient(create_app(service=service, config=config, runtime_manager=manager))


def _project(client):
    return client.post("/api/projects", json={"name": "Demo"}).json()


def test_create_runtime(tmp_path):
    client = _client(tmp_path)
    project = _project(client)

    response = client.post(f"/api/projects/{project['project_id']}/runtime")

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_id"].startswith("rt_")
    assert body["state"] == "created"
    assert "sandbox_id" not in body
    assert "provider_name" not in body


def test_create_runtime_twice_returns_409(tmp_path):
    client = _client(tmp_path)
    project = _project(client)
    client.post(f"/api/projects/{project['project_id']}/runtime")

    response = client.post(f"/api/projects/{project['project_id']}/runtime")

    assert response.status_code == 409
    assert response.json()["error_code"] == "RUNTIME_BUSY"


def test_create_runtime_on_missing_project_is_404(tmp_path):
    client = _client(tmp_path)
    response = client.post("/api/projects/prj_missing/runtime")
    assert response.status_code == 404


def test_start_runtime_auto_creates_when_none_exists(tmp_path):
    client = _client(tmp_path)
    project = _project(client)

    response = client.post(f"/api/projects/{project['project_id']}/runtime/start")

    assert response.status_code == 200
    assert response.json()["state"] == "running"


def test_get_runtime_reflects_current_state(tmp_path):
    client = _client(tmp_path)
    project = _project(client)
    client.post(f"/api/projects/{project['project_id']}/runtime/start")

    response = client.get(f"/api/projects/{project['project_id']}/runtime")

    assert response.status_code == 200
    assert response.json()["state"] == "running"


def test_get_runtime_before_any_creation_is_404(tmp_path):
    client = _client(tmp_path)
    project = _project(client)

    response = client.get(f"/api/projects/{project['project_id']}/runtime")

    assert response.status_code == 404
    assert response.json()["error_code"] == "RUNTIME_NOT_FOUND"


def test_stop_runtime(tmp_path):
    client = _client(tmp_path)
    project = _project(client)
    client.post(f"/api/projects/{project['project_id']}/runtime/start")

    response = client.post(f"/api/projects/{project['project_id']}/runtime/stop")

    assert response.status_code == 200
    assert response.json()["state"] == "stopped"


def test_restart_runtime_produces_a_new_runtime_id(tmp_path):
    client = _client(tmp_path)
    project = _project(client)
    started = client.post(f"/api/projects/{project['project_id']}/runtime/start").json()

    response = client.post(f"/api/projects/{project['project_id']}/runtime/restart")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "running"
    assert body["runtime_id"] != started["runtime_id"]


def test_restart_without_ever_starting_is_404(tmp_path):
    client = _client(tmp_path)
    project = _project(client)

    response = client.post(f"/api/projects/{project['project_id']}/runtime/restart")

    assert response.status_code == 404


def test_start_failure_is_reported_as_a_normal_error_response(tmp_path):
    provider = FakeSandboxProvider(never_healthy=True)
    client = _client(tmp_path, sandbox_provider=provider, max_startup_seconds=0.2,
                      health_poll_interval=0.05)
    project = _project(client)

    response = client.post(f"/api/projects/{project['project_id']}/runtime/start")

    assert response.status_code == 500
    assert response.json()["error_code"] == "RUNTIME_HEALTHCHECK_FAILED"


def test_no_runtime_endpoint_exposes_sandbox_internals(tmp_path):
    """Defensive sweep: sandbox_id, provider_name, container id and host
    paths must never appear in any runtime API response."""
    client = _client(tmp_path)
    project = _project(client)

    responses = [
        client.post(f"/api/projects/{project['project_id']}/runtime/start"),
        client.get(f"/api/projects/{project['project_id']}/runtime"),
        client.post(f"/api/projects/{project['project_id']}/runtime/restart"),
    ]

    for response in responses:
        body = response.json()
        assert "sandbox_id" not in body
        assert "provider_name" not in body
        assert "container" not in response.text.lower()

    tmp_str = str(tmp_path).lower()
    for response in responses:
        assert tmp_str not in response.text.lower()


def test_health_reports_the_runtime_provider_honestly(tmp_path):
    client = _client(tmp_path)

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["runtime"]["provider"] == "fake"
    assert body["runtime"]["isolated"] is False   # FakeSandboxProvider never claims isolation
