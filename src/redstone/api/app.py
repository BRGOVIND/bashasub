"""The minimal Redstone agent API.

Deliberately small: just enough to prove the agent loop end to end over HTTP.
Separate from the existing ``main.py`` (the deployed BhashaSub translation
service), which this does not touch or import — this app is not wired into
production and exists for Phase 3 verification.

`workspace_id` never appears in a request body: the client addresses a
project, and the server looks up its workspace internally. There is no field
anywhere in these schemas for a caller to name a workspace directly.

Task execution is synchronous in this phase: `POST .../agent` runs the whole
bounded loop and returns only once the task reaches a terminal state. A
background task queue is a Phase 4+ concern; documented as a simplification in
docs/redstone/AGENT.md, not hidden.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config import RedstoneConfig, load_config
from .service_provider import get_runtime_manager, get_service, set_runtime_manager, set_service
from ..agent.errors import AgentErrorCode, RedstoneAgentError
from ..agent.service import AgentService
from ..runtime.errors import RedstoneRuntimeError, RuntimeErrorCode
from ..runtime.manager import RuntimeManager
from ..runtime.models import TERMINAL_RUNTIME_STATES
from ..sandbox.models import ResourceLimits
from ..sandbox.providers.registry import best_available_provider

__all__ = ["create_app"]

_STATUS_FOR_CODE = {
    AgentErrorCode.NOT_FOUND: 404,
    AgentErrorCode.PROJECT_NOT_FOUND: 404,
    AgentErrorCode.WORKSPACE_BUSY: 409,
    AgentErrorCode.INVALID_REQUEST: 400,
    AgentErrorCode.INVALID_TRANSITION: 409,
}

# Anything not listed here is a genuine server-side failure (sandbox create/
# start/stop/destroy failed, a resource limit was hit, ...), not a client
# mistake -- those fall through to the 500 default below.
_RUNTIME_STATUS_FOR_CODE = {
    RuntimeErrorCode.NOT_FOUND: 404,
    RuntimeErrorCode.NOT_OWNED: 404,   # never confirm a runtime exists for another project
    RuntimeErrorCode.BUSY: 409,
    RuntimeErrorCode.INVALID_TRANSITION: 409,
    RuntimeErrorCode.INVALID_REQUEST: 400,
}


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AgentRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


def create_app(
    service: AgentService | None = None,
    config: RedstoneConfig | None = None,
    runtime_manager: RuntimeManager | None = None,
) -> FastAPI:
    app = FastAPI(title="Redstone Agent API")
    resolved_config = config or load_config()
    set_service(app, service or AgentService(resolved_config))

    if runtime_manager is None:
        provider = best_available_provider()
        limits = resolved_config.limits
        runtime_manager = RuntimeManager(
            provider,
            max_startup_seconds=limits.max_runtime_startup_seconds,
            health_check_timeout=limits.runtime_health_check_timeout_seconds,
            stop_grace_seconds=limits.runtime_stop_grace_seconds,
            resource_limits=ResourceLimits(
                cpu_cores=limits.sandbox_cpu_cores,
                memory_mb=limits.sandbox_memory_mb,
                pids=limits.sandbox_pids,
                timeout_seconds=limits.max_runtime_timeout,
                output_bytes=limits.sandbox_output_bytes,
            ),
        )
    set_runtime_manager(app, runtime_manager)

    @app.exception_handler(RedstoneAgentError)
    async def handle_agent_error(request: Request, exc: RedstoneAgentError):
        status_code = _STATUS_FOR_CODE.get(exc.code, 500)
        return JSONResponse(status_code=status_code, content=exc.to_dict())

    @app.exception_handler(RedstoneRuntimeError)
    async def handle_runtime_error(request: Request, exc: RedstoneRuntimeError):
        status_code = _RUNTIME_STATUS_FOR_CODE.get(exc.code, 500)
        return JSONResponse(status_code=status_code, content=exc.to_dict())

    @app.get("/api/health")
    async def health():
        manager = get_runtime_manager(app)
        provider = manager.provider
        return {
            "status": "ok",
            "runtime": {
                "provider": provider.name,
                # Never claim isolation the environment cannot actually enforce.
                "isolated": getattr(provider, "is_isolated", False),
            },
        }

    @app.post("/api/projects")
    async def create_project(payload: CreateProjectRequest, request: Request):
        svc = get_service(request.app)
        project = svc.create_project(payload.name)
        return {
            "project_id": project.id,
            "workspace_id": project.workspace_id,
            "status": project.status.value,
        }

    @app.post("/api/projects/{project_id}/agent")
    async def start_agent_task(project_id: str, payload: AgentRequest, request: Request):
        svc = get_service(request.app)
        task = svc.start_task(project_id, payload.message)
        return {"task_id": task.id, "status": task.status.value}

    @app.get("/api/agent/tasks/{task_id}")
    async def get_task(task_id: str, request: Request):
        svc = get_service(request.app)
        task = svc.get_task(task_id)
        return task.to_dict()

    @app.get("/api/agent/tasks/{task_id}/events")
    async def get_task_events(task_id: str, request: Request):
        svc = get_service(request.app)
        return {"events": svc.get_task_events(task_id)}

    # ---------------------------------------------------------------- runtime
    #
    # workspace_id is never accepted from a caller here either: every handler
    # resolves project_id -> Project -> Workspace through AgentService, which
    # is the one place allowed to reach a project's real filesystem path.
    # Responses are always Runtime.to_dict() -- no sandbox_id, no container
    # id, no host path, no provider name.

    def _latest_runtime(manager: RuntimeManager, project_id: str):
        runtimes = manager.list_for_project(project_id)
        if not runtimes:
            raise RedstoneRuntimeError(RuntimeErrorCode.NOT_FOUND)
        active = [r for r in runtimes if r.state not in TERMINAL_RUNTIME_STATES]
        return active[0] if active else max(runtimes, key=lambda r: r.created_at)

    @app.post("/api/projects/{project_id}/runtime")
    async def create_runtime(project_id: str, request: Request):
        svc = get_service(request.app)
        manager = get_runtime_manager(request.app)
        project = svc.get_project(project_id)
        workspace = svc.get_workspace(project_id)
        runtime = manager.create(project_id, workspace, project.framework)
        return runtime.to_dict()

    @app.get("/api/projects/{project_id}/runtime")
    async def get_runtime(project_id: str, request: Request):
        svc = get_service(request.app)
        manager = get_runtime_manager(request.app)
        svc.get_project(project_id)   # 404s if the project itself is unknown
        return _latest_runtime(manager, project_id).to_dict()

    @app.post("/api/projects/{project_id}/runtime/start")
    async def start_runtime(project_id: str, request: Request):
        svc = get_service(request.app)
        manager = get_runtime_manager(request.app)
        project = svc.get_project(project_id)
        workspace = svc.get_workspace(project_id)

        runtimes = manager.list_for_project(project_id)
        active = [r for r in runtimes if r.state not in TERMINAL_RUNTIME_STATES]
        runtime = active[0] if active else manager.create(project_id, workspace, project.framework)

        started = manager.start(runtime.id, project_id, workspace)
        return started.to_dict()

    @app.post("/api/projects/{project_id}/runtime/stop")
    async def stop_runtime(project_id: str, request: Request):
        svc = get_service(request.app)
        manager = get_runtime_manager(request.app)
        svc.get_project(project_id)
        runtime = _latest_runtime(manager, project_id)
        stopped = manager.stop(runtime.id, project_id)
        return stopped.to_dict()

    @app.post("/api/projects/{project_id}/runtime/restart")
    async def restart_runtime(project_id: str, request: Request):
        svc = get_service(request.app)
        manager = get_runtime_manager(request.app)
        svc.get_project(project_id)
        workspace = svc.get_workspace(project_id)
        runtime = _latest_runtime(manager, project_id)
        restarted = manager.restart(runtime.id, project_id, workspace)
        return restarted.to_dict()

    return app
