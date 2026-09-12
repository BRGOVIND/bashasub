"""Normalised sandbox errors.

Mirrors redstone.ai.errors / redstone.agent.errors: a fixed code allowlist, a
Redstone-authored safe message, and no upstream detail (a Docker daemon error
body, a host path, a command line containing a secret) ever crossing the
boundary to a caller.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["SandboxErrorCode", "RedstoneSandboxError"]


class SandboxErrorCode(str, Enum):
    CREATE_FAILED = "SANDBOX_CREATE_FAILED"
    START_FAILED = "SANDBOX_START_FAILED"
    EXEC_FAILED = "SANDBOX_EXEC_FAILED"
    STOP_FAILED = "SANDBOX_STOP_FAILED"
    DESTROY_FAILED = "SANDBOX_DESTROY_FAILED"
    TIMEOUT = "SANDBOX_TIMEOUT"
    NOT_FOUND = "SANDBOX_NOT_FOUND"
    PROVIDER_UNAVAILABLE = "SANDBOX_PROVIDER_UNAVAILABLE"
    OUTPUT_TOO_LARGE = "SANDBOX_OUTPUT_TOO_LARGE"
    INVALID_CONFIG = "SANDBOX_INVALID_CONFIG"
    UNSUPPORTED_OPERATION = "SANDBOX_UNSUPPORTED_OPERATION"


_SAFE_MESSAGES = {
    SandboxErrorCode.CREATE_FAILED: "The sandbox could not be created.",
    SandboxErrorCode.START_FAILED: "The sandbox could not be started.",
    SandboxErrorCode.EXEC_FAILED: "The operation inside the sandbox failed.",
    SandboxErrorCode.STOP_FAILED: "The sandbox could not be stopped.",
    SandboxErrorCode.DESTROY_FAILED: "The sandbox could not be destroyed.",
    SandboxErrorCode.TIMEOUT: "The sandbox operation timed out.",
    SandboxErrorCode.NOT_FOUND: "The sandbox was not found.",
    SandboxErrorCode.PROVIDER_UNAVAILABLE: "The sandbox provider is unavailable.",
    SandboxErrorCode.OUTPUT_TOO_LARGE: "The sandbox produced too much output.",
    SandboxErrorCode.INVALID_CONFIG: "The sandbox configuration was invalid.",
    SandboxErrorCode.UNSUPPORTED_OPERATION: "That operation is not supported by this provider.",
}


class RedstoneSandboxError(Exception):
    def __init__(
        self,
        code: SandboxErrorCode,
        *,
        safe_message: str | None = None,
        internal: str | None = None,
    ) -> None:
        self.code = code
        self.safe_message = safe_message or _SAFE_MESSAGES[code]
        # Server-side diagnosis only. Never rendered by __str__/to_dict. Callers
        # are responsible for redacting secrets before setting this, exactly as
        # the AI gateway's RedstoneAIError.internal already requires.
        self.internal = internal
        super().__init__(self.safe_message)

    def to_dict(self) -> dict:
        return {"error_code": self.code.value, "message": self.safe_message}

    def __repr__(self) -> str:
        return f"RedstoneSandboxError({self.code.value})"
