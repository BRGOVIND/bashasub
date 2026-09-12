"""Normalised runtime errors. Same pattern as agent.errors / ai.errors / sandbox.errors."""

from __future__ import annotations

from enum import Enum

__all__ = ["RuntimeErrorCode", "RedstoneRuntimeError"]


class RuntimeErrorCode(str, Enum):
    NOT_FOUND = "RUNTIME_NOT_FOUND"
    NOT_OWNED = "RUNTIME_NOT_OWNED"
    BUSY = "RUNTIME_BUSY"
    CREATE_FAILED = "RUNTIME_CREATE_FAILED"
    START_FAILED = "RUNTIME_START_FAILED"
    STOP_FAILED = "RUNTIME_STOP_FAILED"
    TIMEOUT = "RUNTIME_TIMEOUT"
    RESOURCE_LIMIT = "RUNTIME_RESOURCE_LIMIT"
    NETWORK_DENIED = "RUNTIME_NETWORK_DENIED"
    HEALTHCHECK_FAILED = "RUNTIME_HEALTHCHECK_FAILED"
    DESTROY_FAILED = "RUNTIME_DESTROY_FAILED"
    INVALID_TRANSITION = "RUNTIME_INVALID_TRANSITION"
    INVALID_REQUEST = "RUNTIME_INVALID_REQUEST"


_SAFE_MESSAGES = {
    RuntimeErrorCode.NOT_FOUND: "Runtime not found.",
    RuntimeErrorCode.NOT_OWNED: "This runtime does not belong to that project.",
    RuntimeErrorCode.BUSY: "This workspace already has an active runtime.",
    RuntimeErrorCode.CREATE_FAILED: "The runtime could not be created.",
    RuntimeErrorCode.START_FAILED: "The runtime could not be started.",
    RuntimeErrorCode.STOP_FAILED: "The runtime could not be stopped.",
    RuntimeErrorCode.TIMEOUT: "The runtime operation timed out.",
    RuntimeErrorCode.RESOURCE_LIMIT: "The runtime exceeded a resource limit.",
    RuntimeErrorCode.NETWORK_DENIED: "That network access is not permitted.",
    RuntimeErrorCode.HEALTHCHECK_FAILED: "The runtime did not become healthy in time.",
    RuntimeErrorCode.DESTROY_FAILED: "The runtime could not be destroyed.",
    RuntimeErrorCode.INVALID_TRANSITION: "The runtime cannot change state that way.",
    RuntimeErrorCode.INVALID_REQUEST: "The request was invalid.",
}


class RedstoneRuntimeError(Exception):
    def __init__(self, code: RuntimeErrorCode, *, safe_message: str | None = None,
                internal: str | None = None) -> None:
        self.code = code
        self.safe_message = safe_message or _SAFE_MESSAGES[code]
        self.internal = internal   # server-side diagnosis only; never rendered
        super().__init__(self.safe_message)

    def to_dict(self) -> dict:
        return {"error_code": self.code.value, "message": self.safe_message}

    def __repr__(self) -> str:
        return f"RedstoneRuntimeError({self.code.value})"
