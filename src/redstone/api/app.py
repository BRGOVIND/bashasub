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
from .service_provider import get_service, set_service
from ..agent.errors import AgentErrorCode, RedstoneAgentError
from ..agent.service import AgentService

__all__ = ["create_app"]

_STATUS_FOR_CODE = {
    AgentErrorCode.NOT_FOUND: 404,
    AgentErrorCode.PROJECT_NOT_FOUND: 404,
    AgentErrorCode.WORKSPACE_BUSY: 409,
    AgentErrorCode.INVALID_REQUEST: 400,
    AgentErrorCode.INVALID_TRANSITION: 409,
}


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AgentRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


def create_app(service: AgentService | None = None, config: RedstoneConfig | None = None) -> FastAPI:
    app = FastAPI(title="Redstone Agent API")
    set_service(app, service or AgentService(config or load_config()))

    @app.exception_handler(RedstoneAgentError)
    async def handle_agent_error(request: Request, exc: RedstoneAgentError):
        status_code = _STATUS_FOR_CODE.get(exc.code, 500)
        return JSONResponse(status_code=status_code, content=exc.to_dict())

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

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

    return app
