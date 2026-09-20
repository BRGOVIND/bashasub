"""Ephemeral request credential passed through an agent run, never task state."""

from __future__ import annotations

from dataclasses import dataclass

from ..ai.models import Credential, CredentialSource
from ..ai.providers.registry import BYOK_PROVIDERS
from .errors import AgentErrorCode, RedstoneAgentError


@dataclass(frozen=True, slots=True, repr=False)
class EphemeralBYOK:
    provider: str
    model: str
    credential: Credential
    base_url: str | None = None

    @classmethod
    def from_payload(cls, payload: object) -> EphemeralBYOK:
        def invalid() -> RedstoneAgentError:
            return RedstoneAgentError(
                AgentErrorCode.INVALID_REQUEST,
                safe_message="Invalid BYOK provider, model, key, or base URL.",
            )

        if not isinstance(payload, dict) or set(payload) - {
            "provider", "model", "api_key", "base_url"
        }:
            raise invalid()
        provider = payload.get("provider")
        model = payload.get("model")
        api_key = payload.get("api_key")
        base_url = payload.get("base_url")
        if (not isinstance(provider, str) or provider not in BYOK_PROVIDERS
                or not isinstance(model, str) or not 1 <= len(model) <= 128
                or not model.strip() or not isinstance(api_key, str)
                or not 8 <= len(api_key) <= 4096 or not api_key.strip()
                or (base_url is not None and (
                    not isinstance(base_url, str) or not 1 <= len(base_url) <= 2048
                ))):
            raise invalid()
        return cls(
            provider=provider,
            model=model,
            credential=Credential(api_key, CredentialSource.REQUEST_BYOK),
            base_url=base_url,
        )

    def __repr__(self) -> str:
        return "EphemeralBYOK(credential=<hidden>)"
