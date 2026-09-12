"""Shared test doubles for the agent test suite.

FakeAIGateway implements the same `generate(request, **kwargs) -> AIResponse`
surface as `redstone.ai.AIGateway`, so it is a drop-in replacement wherever the
agent code depends on a gateway. It never makes a network call and needs no
API key. Every test in this suite uses one of these, never a real provider.
"""

from __future__ import annotations

import json
import time

from redstone.ai.models import AIResponse
from redstone.ai.errors import AIErrorCode, RedstoneAIError

__all__ = [
    "action",
    "FakeAIGateway",
    "FailingAIGateway",
    "FakeValidationRunner",
]


def action(kind: str, **fields) -> str:
    """Build one JSON action-envelope string, as the model is expected to reply."""
    return json.dumps({"action": kind, **fields})


class FakeAIGateway:
    """Replays a fixed script of response texts, one per call.

    Recording every request lets a test assert on exactly what the agent sent
    (e.g. that the system prompt is always first, or that a secret never
    appears anywhere in the conversation).
    """

    def __init__(self, script: list[str] | None = None, delay: float = 0.0,
                 default: str | None = None) -> None:
        self.script = list(script or [])
        self.delay = delay
        self.default = default
        self.requests: list = []

    def generate(self, request, **kwargs) -> AIResponse:
        self.requests.append(request)
        if self.delay:
            time.sleep(self.delay)
        if self.script:
            text = self.script.pop(0)
        elif self.default is not None:
            text = self.default
        else:
            text = action("complete", summary="nothing left to do")
        return AIResponse(text=text, provider="fake", model="fake-model",
                          request_id=request.request_id)


class FailingAIGateway:
    """Always raises a scripted RedstoneAIError, to test AI-failure handling."""

    def __init__(self, code: AIErrorCode = AIErrorCode.PROVIDER_UNAVAILABLE) -> None:
        self.code = code
        self.requests: list = []

    def generate(self, request, **kwargs) -> AIResponse:
        self.requests.append(request)
        raise RedstoneAIError(self.code, request_id=request.request_id)


class FakeValidationRunner:
    """A scripted validation runner, for testing the loop's VALIDATING path.

    This exists ONLY in tests. Production wiring always uses
    UnavailableValidationRunner, which performs no execution.
    """

    def __init__(self, typecheck_ok=True, lint_ok=True, build_ok=True) -> None:
        self._results = {
            "typecheck": typecheck_ok, "lint": lint_ok, "build": build_ok,
        }

    def _result(self, name, ok):
        from redstone.agent.tools.validation import ValidationResult, ValidationStatus
        status = ValidationStatus.PASSED if ok else ValidationStatus.FAILED
        message = f"{name} {'passed' if ok else 'failed'}"
        return ValidationResult(status, message, output="" if ok else "fake error output")

    def run_typecheck(self, project_root):
        return self._result("typecheck", self._results["typecheck"])

    def run_lint(self, project_root):
        return self._result("lint", self._results["lint"])

    def run_build(self, project_root):
        return self._result("build", self._results["build"])
