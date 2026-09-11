"""Normalised AI errors.

Every failure the gateway can produce is one of these codes, from an explicit
allowlist. Upstream HTTP status codes, provider exception strings and HTTP
client internals are translated into a code and a message *Redstone* authored;
none of them cross the boundary to a caller. This is what lets the future
coding agent handle failures without knowing anything about httpx or Gemini.

Standard library only.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["AIErrorCode", "RedstoneAIError"]


class AIErrorCode(str, Enum):
    AUTHENTICATION_FAILED = "AI_AUTHENTICATION_FAILED"
    FORBIDDEN = "AI_FORBIDDEN"
    RATE_LIMITED = "AI_RATE_LIMITED"
    TIMEOUT = "AI_TIMEOUT"
    NETWORK_ERROR = "AI_NETWORK_ERROR"
    PROVIDER_ERROR = "AI_PROVIDER_ERROR"
    INVALID_REQUEST = "AI_INVALID_REQUEST"
    INVALID_MODEL = "AI_INVALID_MODEL"
    MALFORMED_RESPONSE = "AI_MALFORMED_RESPONSE"
    NOT_CONFIGURED = "AI_NOT_CONFIGURED"
    PROVIDER_UNAVAILABLE = "AI_PROVIDER_UNAVAILABLE"
    RESPONSE_TOO_LARGE = "AI_RESPONSE_TOO_LARGE"
    REQUEST_TOO_LARGE = "AI_REQUEST_TOO_LARGE"


# A safe, generic sentence per code. The caller sees this, never upstream text.
_SAFE_MESSAGES = {
    AIErrorCode.AUTHENTICATION_FAILED: "AI provider authentication failed.",
    AIErrorCode.FORBIDDEN: "The AI provider refused the request.",
    AIErrorCode.RATE_LIMITED: "The AI provider is rate limiting requests.",
    AIErrorCode.TIMEOUT: "The AI provider timed out.",
    AIErrorCode.NETWORK_ERROR: "The AI provider could not be reached.",
    AIErrorCode.PROVIDER_ERROR: "The AI provider returned an error.",
    AIErrorCode.INVALID_REQUEST: "The request to the AI provider was invalid.",
    AIErrorCode.INVALID_MODEL: "The requested model is not available.",
    AIErrorCode.MALFORMED_RESPONSE: "The AI provider returned an unreadable response.",
    AIErrorCode.NOT_CONFIGURED: "No AI provider is configured.",
    AIErrorCode.PROVIDER_UNAVAILABLE: "The AI provider is unavailable.",
    AIErrorCode.RESPONSE_TOO_LARGE: "The AI provider response was too large.",
    AIErrorCode.REQUEST_TOO_LARGE: "The AI request was too large.",
}

# Which codes are worth retrying. Authentication and malformed-request failures
# never are: retrying a bad key or a bad request only wastes the user's quota.
_RETRYABLE = frozenset(
    {
        AIErrorCode.RATE_LIMITED,
        AIErrorCode.TIMEOUT,
        AIErrorCode.NETWORK_ERROR,
        AIErrorCode.PROVIDER_UNAVAILABLE,
        AIErrorCode.PROVIDER_ERROR,
    }
)


class RedstoneAIError(Exception):
    """The only exception the gateway raises.

    `safe_message` defaults to the generic sentence for the code, so a caller
    that surfaces it can never accidentally leak upstream detail. An optional
    `internal` note is kept for server-side logging only and is never part of
    the string representation or the wire form.
    """

    def __init__(
        self,
        code: AIErrorCode,
        *,
        request_id: str | None = None,
        safe_message: str | None = None,
        internal: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        self.code = code
        self.request_id = request_id
        self.safe_message = safe_message or _SAFE_MESSAGES[code]
        self.retryable = _RETRYABLE.__contains__(code) if retryable is None else retryable
        # For logs only. Never rendered by __str__ or to_dict.
        self.internal = internal
        super().__init__(self.safe_message)

    @property
    def is_retryable(self) -> bool:
        return self.retryable

    def to_dict(self) -> dict:
        return {
            "error_code": self.code.value,
            "message": self.safe_message,
            "retryable": self.retryable,
            "request_id": self.request_id,
        }

    def __repr__(self) -> str:
        return f"RedstoneAIError({self.code.value}, retryable={self.retryable})"
