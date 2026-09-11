"""The AI gateway.

The one entry point the rest of Redstone uses to reach a model. It resolves the
provider and credential, enforces the request-size ceiling, runs the request
with bounded retries, and returns a normalised AIResponse or raises a
RedstoneAIError. Callers never see httpx, a provider SDK, or a raw HTTP status.

Credential resolution has two modes that are never mixed silently:
  * a credential supplied on the request (BYOK) always takes precedence;
  * otherwise the server-configured key is used.
A BYOK request is additionally constrained to allowlisted provider hosts.
"""

from __future__ import annotations

import random
import time

from ..config import AIConfig
from ..domain.models import new_id
from .errors import AIErrorCode, RedstoneAIError
from .models import AIRequest, AIResponse, Credential, CredentialSource
from .providers.registry import BYOK_PROVIDERS, get_provider
from .redaction import redact
from .ssrf import BaseUrlError, validate_base_url, validate_byok_host

__all__ = ["AIGateway"]


class AIGateway:
    """Provider-agnostic generation with BYOK, retries and safe errors."""

    def __init__(self, config: AIConfig, *, logger=None, transport=None) -> None:
        self._config = config
        self._logger = logger
        # A test transport (httpx.MockTransport) is threaded down to the
        # provider so the real request/serialisation path runs without network.
        self._transport = transport

    # ------------------------------------------------------------- resolution

    def _resolve_credential(
        self,
        request_id: str,
        byok_key: str | None,
    ) -> Credential:
        if byok_key:
            return Credential(byok_key, CredentialSource.REQUEST_BYOK)
        if self._config.api_key:
            return Credential(self._config.api_key, CredentialSource.SERVER_CONFIGURED)
        raise RedstoneAIError(AIErrorCode.NOT_CONFIGURED, request_id=request_id)

    def _resolve_base_url(
        self, provider_name: str, base_url: str | None, *, byok: bool, request_id: str
    ) -> str | None:
        """Validate and return the base URL, or None to use the provider default."""
        if provider_name == "gemini" and not base_url:
            return None  # provider default; a fixed Google endpoint
        if not base_url:
            # openai-compatible with no URL falls back to api.openai.com default.
            return None
        try:
            return validate_byok_host(base_url) if byok else validate_base_url(base_url)
        except BaseUrlError as exc:
            raise RedstoneAIError(
                AIErrorCode.INVALID_REQUEST,
                request_id=request_id,
                safe_message=str(exc),
            )

    # ---------------------------------------------------------------- generate

    def generate(
        self,
        request: AIRequest,
        *,
        provider: str | None = None,
        model: str | None = None,
        byok_key: str | None = None,
        base_url: str | None = None,
    ) -> AIResponse:
        request_id = request.request_id if request.request_id != "-" else new_id("air")
        request = request if request.request_id != "-" else _with_id(request, request_id)

        provider_name = (provider or self._config.provider or "").strip().lower()
        model_name = model or request.model or self._config.model
        byok = byok_key is not None

        if byok and provider_name not in BYOK_PROVIDERS:
            raise RedstoneAIError(
                AIErrorCode.INVALID_REQUEST,
                request_id=request_id,
                safe_message="This provider is not permitted for user-supplied keys.",
            )

        if request.approximate_size() > self._config.max_request_bytes:
            raise RedstoneAIError(AIErrorCode.REQUEST_TOO_LARGE, request_id=request_id)

        adapter = get_provider(provider_name, request_id=request_id)
        credential = self._resolve_credential(request_id, byok_key)
        resolved_base = self._resolve_base_url(
            provider_name, base_url or self._config.base_url or None,
            byok=byok, request_id=request_id,
        )

        return self._run_with_retries(
            adapter, request, credential,
            model=model_name, base_url=resolved_base,
            provider_name=provider_name, request_id=request_id,
        )

    # ------------------------------------------------------------------ retry

    def _run_with_retries(
        self, adapter, request, credential, *, model, base_url, provider_name, request_id
    ) -> AIResponse:
        attempts = max(1, self._config.max_retries + 1)
        last: RedstoneAIError | None = None

        for attempt in range(attempts):
            started = time.monotonic()
            try:
                response = adapter.generate(
                    request,
                    credential,
                    model=model,
                    base_url=base_url,
                    timeout=float(self._config.timeout_seconds),
                    max_response_bytes=self._config.max_response_bytes,
                    transport=self._transport,
                )
                self._log(provider_name, model, request_id, "ok",
                          time.monotonic() - started, attempt, None)
                return response
            except RedstoneAIError as exc:
                last = exc
                self._log(provider_name, model, request_id,
                          "error", time.monotonic() - started, attempt, exc.code)
                # Stop immediately on anything not worth retrying (auth, invalid
                # request/model, too-large), or once attempts are exhausted.
                if not exc.retryable or attempt == attempts - 1:
                    raise
                self._sleep(attempt)

        raise last if last else RedstoneAIError(
            AIErrorCode.PROVIDER_ERROR, request_id=request_id
        )

    def _sleep(self, attempt: int) -> None:
        base = self._config.retry_backoff_seconds * (2 ** attempt)
        # Full jitter, so simultaneous retries do not synchronise.
        time.sleep(base + random.uniform(0, base))

    # ------------------------------------------------------------------- log

    def _log(self, provider, model, request_id, status, duration, attempt, code) -> None:
        if self._logger is None:
            return
        # Only safe scalar fields. No prompt, no response, no credential. The
        # credential value never appears here, but redact() is applied to the
        # code as belt-and-braces in case an adapter ever builds a custom
        # message from upstream text.
        api_key = self._config.api_key
        self._logger.info(
            "ai_request "
            f"request_id={request_id} provider={provider} model={model} "
            f"status={status} duration_ms={int(duration * 1000)} attempt={attempt} "
            f"error_code={redact(code.value if code else '-', api_key)}"
        )


def _with_id(request: AIRequest, request_id: str) -> AIRequest:
    from dataclasses import replace

    return replace(request, request_id=request_id)
