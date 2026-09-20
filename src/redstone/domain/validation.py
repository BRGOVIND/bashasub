"""Neutral validation result shared by agent tools and sandbox runtime."""

from __future__ import annotations

from dataclasses import dataclass


class ValidationStatus:
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    status: str
    message: str
    output: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "message": self.message, "output": self.output}
