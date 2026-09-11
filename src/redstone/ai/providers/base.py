"""Provider contract and the shared HTTP helper.

Every adapter translates the neutral AIRequest into its provider's wire format,
calls :func:`post_json`, and translates the response back. The HTTP helper is
where the transport-level security lives, in one place: explicit timeout,
redirects disabled (so an Authorization header can never follow a redirect to
another host), a hard response-size cap read incrementally, and translation of
every transport failure into a RedstoneAIError with the credential scrubbed
from any exception text.

httpx is imported lazily inside the helper so importing the provider package
does not require the optional `ai` extra until a request is actually made.
"""

from __future__ import annotations

import json
from typing import Protocol

from ..errors import AIErrorCode, RedstoneAIError
from ..models import AIRequest, AIResponse, Credential
from ..redaction import redact_exception

__all__ = ["AIProvider", "post_json", "status_to_error_code"]


class AIProvider(Protocol):
    """What the gateway depends on. No provider-specific type escapes this."""

    name: str

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
    ) -> AIResponse: ...


def status_to_error_code(status: int) -> AIErrorCode:
    """Map an HTTP status to a normalised code (shared by all adapters)."""
    if status in (401,):
        return AIErrorCode.AUTHENTICATION_FAILED
    if status in (403,):
        return AIErrorCode.FORBIDDEN
    if status in (400, 422):
        return AIErrorCode.INVALID_REQUEST
    if status == 404:
        return AIErrorCode.INVALID_MODEL
    if status == 429:
        return AIErrorCode.RATE_LIMITED
    if status in (500, 502, 503, 504):
        return AIErrorCode.PROVIDER_UNAVAILABLE
    return AIErrorCode.PROVIDER_ERROR


def post_json(
    url: str,
    headers: dict,
    payload: dict,
    *,
    timeout: float,
    max_response_bytes: int,
    secret: str,
    request_id: str,
    transport=None,
) -> tuple[int, dict]:
    """POST JSON and return (status_code, parsed_body).

    Raises RedstoneAIError for timeout, network failure, an oversized response
    or an unparseable body. A non-2xx status is returned to the caller (with its
    parsed body if any) rather than raised, so the adapter can map it — but the
    raw body is never surfaced to the end caller.
    """
    import httpx

    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,   # never let auth headers follow a redirect
            transport=transport,
        ) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_response_bytes:
                        raise RedstoneAIError(
                            AIErrorCode.RESPONSE_TOO_LARGE, request_id=request_id
                        )
                    chunks.append(chunk)
                body_bytes = b"".join(chunks)
    except RedstoneAIError:
        raise
    except httpx.TimeoutException as exc:
        raise RedstoneAIError(
            AIErrorCode.TIMEOUT,
            request_id=request_id,
            internal=redact_exception(exc, secret),
        )
    except httpx.RequestError as exc:
        # Connection refused, DNS failure, redirect on a stream, TLS error…
        raise RedstoneAIError(
            AIErrorCode.NETWORK_ERROR,
            request_id=request_id,
            internal=redact_exception(exc, secret),
        )

    status = response.status_code

    if not body_bytes:
        return status, {}

    try:
        parsed = json.loads(body_bytes)
    except ValueError as exc:
        # A 2xx with an unparseable body is malformed; a non-2xx without JSON
        # still carries a usable status, so hand back an empty body.
        if 200 <= status < 300:
            raise RedstoneAIError(
                AIErrorCode.MALFORMED_RESPONSE,
                request_id=request_id,
                internal=redact_exception(exc, secret),
            )
        return status, {}

    if not isinstance(parsed, dict):
        if 200 <= status < 300:
            raise RedstoneAIError(AIErrorCode.MALFORMED_RESPONSE, request_id=request_id)
        return status, {}

    return status, parsed
