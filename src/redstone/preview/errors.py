"""Normalised preview errors. Same pattern as runtime/sandbox/agent errors."""

from __future__ import annotations

from enum import Enum

__all__ = ["PreviewErrorCode", "PreviewError"]


class PreviewErrorCode(str, Enum):
    NOT_FOUND = "PREVIEW_NOT_FOUND"
    BUSY = "PREVIEW_BUSY"
    LIMIT_REACHED = "PREVIEW_LIMIT_REACHED"
    START_FAILED = "PREVIEW_START_FAILED"
    UNAVAILABLE = "PREVIEW_UNAVAILABLE"


_SAFE_MESSAGES = {
    PreviewErrorCode.NOT_FOUND: "Preview not found.",
    PreviewErrorCode.BUSY: "This project is busy; try again shortly.",
    PreviewErrorCode.LIMIT_REACHED: "Too many previews are running.",
    PreviewErrorCode.START_FAILED: "The preview could not be started.",
    PreviewErrorCode.UNAVAILABLE: "Live preview is unavailable in this environment.",
}


class PreviewError(Exception):
    def __init__(self, code: PreviewErrorCode, *, safe_message: str | None = None,
                 internal: str | None = None) -> None:
        self.code = code
        self.safe_message = safe_message or _SAFE_MESSAGES[code]
        self.internal = internal   # server-side diagnosis only; never rendered
        super().__init__(self.safe_message)

    def to_dict(self) -> dict:
        return {"error_code": self.code.value, "message": self.safe_message}

    def __repr__(self) -> str:
        return f"PreviewError({self.code.value})"
