"""Tiny indirection over FastAPI's `app.state`, so the rest of the API module
doesn't scatter string-keyed attribute access."""

from __future__ import annotations

from fastapi import FastAPI

from ..agent.service import AgentService
from ..runtime.manager import RuntimeManager

__all__ = ["get_service", "set_service", "get_runtime_manager", "set_runtime_manager"]


def set_service(app: FastAPI, service: AgentService) -> None:
    app.state.agent_service = service


def get_service(app: FastAPI) -> AgentService:
    return app.state.agent_service


def set_runtime_manager(app: FastAPI, manager: RuntimeManager) -> None:
    app.state.runtime_manager = manager


def get_runtime_manager(app: FastAPI) -> RuntimeManager:
    return app.state.runtime_manager
