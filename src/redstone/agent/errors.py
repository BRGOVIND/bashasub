"""Normalised agent errors.

Mirrors the pattern in ``redstone.ai.errors``: a fixed code allowlist, a
Redstone-authored safe message, and no upstream detail (AI exception text,
filesystem exception text, host paths) ever reaching a caller.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["AgentErrorCode", "RedstoneAgentError"]


class AgentErrorCode(str, Enum):
    NOT_FOUND = "AGENT_TASK_NOT_FOUND"
    PROJECT_NOT_FOUND = "AGENT_PROJECT_NOT_FOUND"
    WORKSPACE_BUSY = "AGENT_WORKSPACE_BUSY"
    INVALID_TRANSITION = "AGENT_INVALID_TRANSITION"
    ITERATION_LIMIT = "AGENT_ITERATION_LIMIT"
    TIMEOUT = "AGENT_TIMEOUT"
    CANCELLED = "AGENT_CANCELLED"
    TOOL_NOT_FOUND = "AGENT_TOOL_NOT_FOUND"
    TOOL_INVALID_ARGUMENTS = "AGENT_TOOL_INVALID_ARGUMENTS"
    MALFORMED_ACTION = "AGENT_MALFORMED_ACTION"
    AI_FAILURE = "AGENT_AI_FAILURE"
    INTERNAL_ERROR = "AGENT_INTERNAL_ERROR"
    INVALID_REQUEST = "AGENT_INVALID_REQUEST"


_SAFE_MESSAGES = {
    AgentErrorCode.NOT_FOUND: "Agent task not found.",
    AgentErrorCode.PROJECT_NOT_FOUND: "Project not found.",
    AgentErrorCode.WORKSPACE_BUSY: "Another agent task is already running for this project.",
    AgentErrorCode.INVALID_TRANSITION: "The agent task cannot change state that way.",
    AgentErrorCode.ITERATION_LIMIT: "The agent reached its iteration limit before finishing.",
    AgentErrorCode.TIMEOUT: "The agent task timed out.",
    AgentErrorCode.CANCELLED: "The agent task was cancelled.",
    AgentErrorCode.TOOL_NOT_FOUND: "The agent tried to use a tool that does not exist.",
    AgentErrorCode.TOOL_INVALID_ARGUMENTS: "The agent's tool call had invalid arguments.",
    AgentErrorCode.MALFORMED_ACTION: "The agent produced an unreadable response.",
    AgentErrorCode.AI_FAILURE: "The AI provider could not complete the request.",
    AgentErrorCode.INTERNAL_ERROR: "The agent task failed unexpectedly.",
    AgentErrorCode.INVALID_REQUEST: "The request was invalid.",
}


class RedstoneAgentError(Exception):
    """The only exception the agent layer raises outward."""

    def __init__(
        self,
        code: AgentErrorCode,
        *,
        request_id: str | None = None,
        safe_message: str | None = None,
        internal: str | None = None,
    ) -> None:
        self.code = code
        self.request_id = request_id
        self.safe_message = safe_message or _SAFE_MESSAGES[code]
        self.internal = internal  # server-side logging only; never rendered
        super().__init__(self.safe_message)

    def to_dict(self) -> dict:
        return {
            "error_code": self.code.value,
            "message": self.safe_message,
            "request_id": self.request_id,
        }

    def __repr__(self) -> str:
        return f"RedstoneAgentError({self.code.value})"
