"""Phase 6 preview lifecycle and failure handling -- REAL DOCKER.

Separate module on purpose: the restart-recovery test reconciles orphans,
which (correctly) destroys every Redstone container no manager tracks, so
it must not share a module with long-lived previews.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agent_fakes import FakeAIGateway, action
from preview_support import (
    LiveServer,
    REACT,
    VITE_APP,
    docker,
    free_port,
    make_workspace,
    managed,
    preview_host,
    raw_request,
)
from redstone.agent.service import AgentService
from redstone.api.app import create_app
from redstone.config import AIConfig, Limits, PreviewConfig, RedstoneConfig
from redstone.domain.models import PreviewStatus, RuntimeState
from redstone.preview.errors import PreviewError, PreviewErrorCode
from redstone.preview.gateway import create_preview_gateway_app
from redstone.preview.manager import PreviewManager
from redstone.runtime.manager import RuntimeManager
from redstone.sandbox.providers.docker_provider import DockerSandboxProvider, docker_available
from redstone.sandbox.providers.local_provider import LocalProcessSandboxProvider

pytestmark = pytest.mark.skipif(not docker_available(), reason="Docker daemon not reachable")


class Env:
    pass


@pytest.fixture
def env(tmp_path):
    e = Env()
    e.root = tmp_path
    e.port = free_port()
    e.config = PreviewConfig(public_port=e.port, listen_port=e.port, ready_timeout_seconds=20.0,
                             idle_timeout_seconds=60.0, max_lifetime_seconds=600.0)
    e.runtimes = RuntimeManager(DockerSandboxProvider(), max_startup_seconds=60)
    e.previews = PreviewManager(e.runtimes, e.config)
    e.projects: list[str] = []
    with LiveServer(create_preview_gateway_app(e.previews, e.config), port=e.port):
        yield e
    for project_id in e.projects:
        e.previews.destroy(project_id)


def _start(e, project_id, workspace_id, **kwargs):
    e.projects.append(project_id)
    workspace = make_workspace(e.root, workspace_id, identity=project_id.upper(), **kwargs)
    return workspace, e.previews.start(project_id, workspace, REACT)


def _fetch(e, preview, path="/__whoami"):
    return raw_request(e.port, preview_host(preview.id, e.port), path)


def _names(e, preview, project_id):
    sid = e.runtimes.get(preview.runtime_id, project_id).sandbox_id
    return sid, {sid, f"{sid}-relay"}, {f"{sid}-net", f"{sid}-pub"}


# =========================================================== Y, Z: destroy

def test_destroy_removes_everything_and_is_idempotent(env):
    _, preview = _start(env, "prj_destroy", "ws_destroy")
    sid, containers, networks = _names(env, preview, "prj_destroy")
    assert _fetch(env, preview)[2] == b"PRJ_DESTROY"
    before_c, before_n = managed()
    assert containers <= before_c and networks <= before_n

    assert env.previews.destroy("prj_destroy") is True
    assert env.previews.destroy("prj_destroy") is False       # second destroy is safe

    after_c, after_n = managed()
    assert not containers & after_c and not networks & after_n
    # The container -- and with it the app's whole PID namespace -- is gone.
    assert docker("inspect", sid).returncode != 0
    assert _fetch(env, preview)[0] == 404
    assert env.runtimes.get(preview.runtime_id, "prj_destroy").state is RuntimeState.DESTROYED


# ================================================ U, V, W: stale ids, restart

def test_stop_then_start_issues_a_new_origin_and_the_old_one_dies(env):
    workspace, first = _start(env, "prj_restart", "ws_restart")
    stopped = env.previews.stop("prj_restart")
    assert stopped.status is PreviewStatus.STOPPED
    assert env.previews.stop("prj_restart").status is PreviewStatus.STOPPED   # idempotent
    assert _fetch(env, first)[0] == 503

    second = env.previews.start("prj_restart", workspace, REACT)
    assert second.id != first.id
    assert _fetch(env, first)[0] == 404
    assert _fetch(env, second)[2] == b"PRJ_RESTART"


def test_a_deleted_preview_never_resolves_to_a_later_one(env):
    workspace, first = _start(env, "prj_stale", "ws_stale")
    env.previews.destroy("prj_stale")
    second = env.previews.start("prj_stale", workspace, REACT)
    assert second.id != first.id
    assert _fetch(env, first)[0] == 404
    assert _fetch(env, second)[0] == 200


# ============================================================ failures

def test_app_not_listening_on_the_preview_port_fails_and_cleans_up(env):
    before = managed()
    env.projects.append("prj_wrongport")
    workspace = make_workspace(env.root, "ws_wrongport", extra={"port.txt": "9999"})
    with pytest.raises(PreviewError) as caught:
        env.previews.start("prj_wrongport", workspace, REACT)
    assert caught.value.code is PreviewErrorCode.START_FAILED
    preview = env.previews.get_for_project("prj_wrongport")
    assert preview.status is PreviewStatus.FAILED
    assert env.previews.describe(preview)["url"] is None
    assert managed() == before


def test_unavailable_relay_fails_closed(env, tmp_path):
    before = managed()
    runtimes = RuntimeManager(DockerSandboxProvider(preview_relay_script=tmp_path / "missing.js"),
                              max_startup_seconds=30)
    previews = PreviewManager(runtimes, env.config)
    workspace = make_workspace(tmp_path, "ws_norelay")
    with pytest.raises(PreviewError) as caught:
        previews.start("prj_norelay", workspace, REACT)
    assert caught.value.code is PreviewErrorCode.START_FAILED
    assert managed() == before


def test_a_crashed_preview_is_noticed_and_stops_being_served(env):
    _, preview = _start(env, "prj_crash", "ws_crash")
    raw_request(env.port, preview_host(preview.id, env.port), "/__crash")
    time.sleep(1.5)
    report = env.previews.sweep()
    assert "prj_crash" in report["crashed"]
    assert env.previews.get_for_project("prj_crash").status is PreviewStatus.FAILED
    assert _fetch(env, preview)[0] == 503


def test_memory_exhaustion_kills_only_the_preview(env):
    _, preview = _start(env, "prj_oom", "ws_oom")
    sid, _, _ = _names(env, preview, "prj_oom")
    raw_request(env.port, preview_host(preview.id, env.port), "/__alloc")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        state = docker("inspect", "-f", "{{.State.Running}} {{.State.OOMKilled}}", sid).stdout.split()
        if state and state[0] == "false":
            break
        time.sleep(0.5)
    assert state[0] == "false", "the --memory limit must stop a runaway allocation"
    env.previews.sweep()
    assert env.previews.get_for_project("prj_oom").status is PreviewStatus.FAILED


def test_destroy_during_startup_leaves_nothing_behind(env):
    before = managed()
    workspace = make_workspace(env.root, "ws_racing")
    env.projects.append("prj_racing")
    errors = []

    def start():
        try:
            env.previews.start("prj_racing", workspace, REACT)
        except PreviewError as exc:
            errors.append(exc)

    starter = threading.Thread(target=start)
    starter.start()
    time.sleep(1.5)                                 # mid-startup
    assert env.previews.destroy("prj_racing") is True
    starter.join()
    assert managed() == before
    with pytest.raises(PreviewError):
        env.previews.get_for_project("prj_racing")


# ================================================================ limits

def test_idle_and_lifetime_limits_stop_previews(env):
    workspace, preview = _start(env, "prj_idle", "ws_idle")
    later = preview.last_activity + timedelta(seconds=env.config.idle_timeout_seconds + 5)
    assert "prj_idle" in env.previews.sweep(now=later)["idle"]
    assert env.previews.get_for_project("prj_idle").status is PreviewStatus.STOPPED

    again = env.previews.start("prj_idle", workspace, REACT)
    much_later = again.created_at + timedelta(seconds=env.config.max_lifetime_seconds + 5)
    assert "prj_idle" in env.previews.sweep(now=much_later)["expired"]


def test_concurrent_preview_limit(env, tmp_path):
    runtimes = RuntimeManager(DockerSandboxProvider(), max_startup_seconds=60)
    previews = PreviewManager(runtimes, PreviewConfig(max_active=1, ready_timeout_seconds=20))
    one = make_workspace(tmp_path, "ws_limit_one")
    two = make_workspace(tmp_path, "ws_limit_two")
    try:
        previews.start("prj_limit_one", one, REACT)
        with pytest.raises(PreviewError) as caught:
            previews.start("prj_limit_two", two, REACT)
        assert caught.value.code is PreviewErrorCode.LIMIT_REACHED
    finally:
        previews.destroy("prj_limit_one")


def test_a_provider_that_cannot_isolate_is_refused(tmp_path):
    previews = PreviewManager(RuntimeManager(LocalProcessSandboxProvider()), PreviewConfig())
    workspace = make_workspace(tmp_path, "ws_local")
    with pytest.raises(PreviewError) as caught:
        previews.start("prj_local", workspace, REACT)
    assert caught.value.code is PreviewErrorCode.UNAVAILABLE


# ======================================================= Redstone restart

def test_redstone_restart_leaves_no_preview_behind(env):
    _, preview = _start(env, "prj_reboot", "ws_reboot")
    sid, containers, networks = _names(env, preview, "prj_reboot")

    # A new process: fresh managers, same Docker daemon.
    report = RuntimeManager(DockerSandboxProvider()).reconcile_orphaned_containers()
    assert {sid, f"{sid}-relay"} <= set(report["destroyed"])
    after_c, after_n = managed()
    assert not containers & after_c and not networks & after_n

    fresh = PreviewManager(RuntimeManager(DockerSandboxProvider()), env.config)
    port = free_port()
    with LiveServer(create_preview_gateway_app(fresh, env.config), port=port):
        assert raw_request(port, preview_host(preview.id, port), "/")[0] == 404


# ========================================== the whole chain, through the API

def test_agent_to_preview_end_to_end_through_the_api(env, tmp_path):
    def server(marker):
        return ("require('http').createServer((q,s)=>s.end('" + marker + "'))"
                ".listen(5173,'0.0.0.0')")

    package = json.dumps({"name": "agent-app", "version": "1.0.0", "private": True,
                          "scripts": {"dev": "node server.js"}})
    gateway = FakeAIGateway([
        action("tool_call", tool="write_file", arguments={"path": "package.json", "content": package}),
        action("tool_call", tool="write_file", arguments={"path": "server.js", "content": server("AGENT-V1")}),
        action("complete", summary="built v1"),
        action("tool_call", tool="write_file", arguments={"path": "server.js", "content": server("AGENT-V2")}),
        action("complete", summary="built v2"),
    ])
    config = RedstoneConfig(workspaces_root=tmp_path / "workspaces", limits=Limits(),
                            ai=AIConfig(), preview=env.config)
    service = AgentService(config, gateway=gateway)
    client = TestClient(create_app(service=service, config=config, runtime_manager=env.runtimes,
                                   preview_manager=env.previews))

    project = service.create_project("Demo", REACT)
    env.projects.append(project.id)
    assert service.start_task(project.id, "build it").status.value == "completed"

    started = client.post(f"/api/projects/{project.id}/preview")
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["status"] == "ready"
    first_id = body["preview_id"]
    assert body["url"] == f"http://{first_id}.localhost:{env.port}/"
    upstream = env.runtimes.provider.preview_upstream(
        env.runtimes.get(env.previews.get_for_project(project.id).runtime_id, project.id).sandbox_id)
    assert upstream.token not in started.text and "sandbox" not in started.text
    assert raw_request(env.port, preview_host(first_id, env.port), "/")[2] == b"AGENT-V1"

    assert service.start_task(project.id, "change it").status.value == "completed"
    assert client.post(f"/api/projects/{project.id}/preview/stop").json()["status"] == "stopped"
    second = client.post(f"/api/projects/{project.id}/preview").json()
    assert second["preview_id"] != first_id
    assert raw_request(env.port, preview_host(second["preview_id"], env.port), "/")[2] == b"AGENT-V2"
    assert raw_request(env.port, preview_host(first_id, env.port), "/")[0] == 404

    assert client.delete(f"/api/projects/{project.id}/preview").json() == {"destroyed": True}
    assert client.delete(f"/api/projects/{project.id}/preview").json() == {"destroyed": False}
    assert client.get(f"/api/projects/{project.id}/preview").status_code == 404


# ============================================================ real Vite

def test_a_real_vite_app_is_previewable_and_picks_up_edits(env, tmp_path):
    """A genuine Vite dev server (installed through the Phase 4.2 egress
    proxy), reached through relay + gateway. Vite accepts the request because
    the relay presents Host: localhost:5173."""
    runtimes = RuntimeManager(DockerSandboxProvider(), max_startup_seconds=240)
    previews = PreviewManager(runtimes, PreviewConfig(public_port=env.port, listen_port=env.port,
                                                      ready_timeout_seconds=90))
    workspace = make_workspace(tmp_path, "ws_vite", source=VITE_APP)
    port = free_port()
    try:
        preview = previews.start("prj_vite", workspace, REACT)
        with LiveServer(create_preview_gateway_app(previews, previews.config), port=port):
            host = preview_host(preview.id, port)
            status, _, page = raw_request(port, host, "/")
            assert status == 200 and b"/@vite/client" in page and b"vite preview fixture" in page
            assert b"VITE_MARKER_ONE" in raw_request(port, host, "/main.js")[2]

            (workspace.project_root / "main.js").write_text(
                'document.getElementById("app").textContent = "VITE_MARKER_TWO";\n', encoding="utf-8")
            deadline = time.monotonic() + 20
            body = b""
            while time.monotonic() < deadline:
                body = raw_request(port, host, "/main.js")[2]
                if b"VITE_MARKER_TWO" in body:
                    break
                time.sleep(0.5)
            assert b"VITE_MARKER_TWO" in body, "the preview must pick up edits without a restart"

            # Vite's own /@fs route can only see the container's filesystem.
            status, _, leaked = raw_request(port, host, "/@fs/workspace/outside-secret.txt")
            assert b"HOST-SIDE-SECRET" not in leaked
    finally:
        previews.destroy("prj_vite")
