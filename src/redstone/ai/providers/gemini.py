"""Google Gemini adapter.

Authenticates with the ``x-goog-api-key`` request header — never the ``?key=``
query parameter that the old BhashaSub code used and that leaked the key through
error messages. The key therefore stays out of the URL, out of redirect
targets, and out of httpx's exception strings.
"""

from __future__ import annotations

from ..errors import AIErrorCode, RedstoneAIError
from ..models import AIRequest, AIResponse, Credential
from .base import post_json, status_to_error_code

__all__ = ["GeminiProvider"]

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider:
    name = "gemini"

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
        url = f"{base}/models/{model}:generateContent"

        # System messages become Gemini's systemInstruction; the rest become
        # contents with role user/model.
        system_parts = [m.content for m in request.messages if m.role == "system"]
        contents = [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in request.messages
            if m.role != "system"
        ]

        payload: dict = {"contents": contents}
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}

        generation_config: dict = {}
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_output_tokens
        if generation_config:
            payload["generationConfig"] = generation_config

        headers = {
            "x-goog-api-key": credential.reveal(),   # header auth, not the URL
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
            candidates = body["candidates"]
            parts = candidates[0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts)
            finish = candidates[0].get("finishReason")
        except (KeyError, IndexError, TypeError):
            # A blocked or empty generation has no usable candidate.
            raise RedstoneAIError(AIErrorCode.MALFORMED_RESPONSE, request_id=request_id)

        usage = body.get("usageMetadata") or {}
        return AIResponse(
            text=text,
            provider="gemini",
            model=model,
            request_id=request_id,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
            total_tokens=usage.get("totalTokenCount"),
            finish_reason=finish,
        )
