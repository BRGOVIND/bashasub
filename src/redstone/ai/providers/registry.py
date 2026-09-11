"""Provider registry.

Providers are resolved from a fixed allowlist of names to already-imported
classes. There is no dynamic import and no name-to-module resolution, so a
caller (or a future model) cannot name an arbitrary Python module and have it
imported. An unknown name is a normalised NOT_CONFIGURED error, never an
ImportError with a path in it.
"""

from __future__ import annotations

from ..errors import AIErrorCode, RedstoneAIError
from .base import AIProvider
from .gemini import GeminiProvider
from .openai_compatible import OpenAICompatibleProvider

__all__ = ["get_provider", "known_providers", "BYOK_PROVIDERS"]

_PROVIDERS: dict[str, AIProvider] = {
    "gemini": GeminiProvider(),
    "openai-compatible": OpenAICompatibleProvider(),
}

# Providers a BYOK request may select. Same set for now, but kept separate so
# a server-only provider could exist later without being BYOK-selectable.
BYOK_PROVIDERS = frozenset({"gemini", "openai-compatible"})


def known_providers() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))


def get_provider(name: str, *, request_id: str = "-") -> AIProvider:
    provider = _PROVIDERS.get((name or "").strip().lower())
    if provider is None:
        raise RedstoneAIError(
            AIErrorCode.NOT_CONFIGURED,
            request_id=request_id,
            safe_message=f"Unknown AI provider. Known providers: {', '.join(known_providers())}.",
        )
    return provider
