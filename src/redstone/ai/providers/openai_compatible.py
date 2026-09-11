"""Generic OpenAI-compatible chat-completions adapter.

Works against any endpoint that implements POST /chat/completions with the
OpenAI request/response shape, driven entirely by (base_url, model, key). No
Groq- or OpenRouter-specific code: those are just base URLs configured through
the registry. Authenticates with a Bearer header.
"""

from __future__ import annotations

from ..errors import AIErrorCode, RedstoneAIError
from ..models import AIRequest, AIResponse, Credential
from .base import post_json, status_to_error_code

__all__ = ["OpenAICompatibleProvider"]

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def generate(
        self,
        request: AIRequest,
        credential: Credential,
        *,
        model: str,
        base_url: str | None,
        timeout: float,
        max_response_bytes: int,
        transport=None,
    ) -> AIResponse:
        base = (base_url or DEFAULT_BASE_URL).rstrip("/")
        url = f"{base}/chat/completions"

        payload: dict = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens

        headers = {
            "Authorization": f"Bearer {credential.reveal()}",
            "Content-Type": "application/json",
        }

        status, body = post_json(
            url,
            headers,
            payload,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
            secret=credential.reveal(),
            request_id=request.request_id,
            transport=transport,
        )

        if status < 200 or status >= 300:
            raise RedstoneAIError(status_to_error_code(status), request_id=request.request_id)

        return self._parse(body, model, request.request_id)

    @staticmethod
    def _parse(body: dict, model: str, request_id: str) -> AIResponse:
        try:
            choice = body["choices"][0]
            text = choice["message"]["content"] or ""
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError):
            raise RedstoneAIError(AIErrorCode.MALFORMED_RESPONSE, request_id=request_id)

        usage = body.get("usage") or {}
        return AIResponse(
            text=text,
            provider="openai-compatible",
            model=body.get("model") or model,
            request_id=request_id,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            finish_reason=finish,
        )
