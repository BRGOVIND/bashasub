"""Sandbox provider registry.

Fixed allowlist, exactly like redstone.ai.providers.registry and
redstone.agent.tools.registry: no dynamic import, no name-to-module
resolution. Providers are instantiated lazily so importing this module never
requires Docker to be installed or reachable.
"""

from __future__ import annotations

from ..errors import RedstoneSandboxError, SandboxErrorCode
from .docker_provider import DockerSandboxProvider, docker_available
from .local_provider import LocalProcessSandboxProvider

__all__ = ["get_sandbox_provider", "best_available_provider", "known_providers"]

_PROVIDERS = {
    "docker": DockerSandboxProvider,
    "local_process": LocalProcessSandboxProvider,
}


def known_providers() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))


def get_sandbox_provider(name: str):
    factory = _PROVIDERS.get((name or "").strip().lower())
    if factory is None:
        raise RedstoneSandboxError(
            SandboxErrorCode.PROVIDER_UNAVAILABLE,
            safe_message=f"Unknown sandbox provider. Known: {', '.join(known_providers())}.",
        )
    return factory()


def best_available_provider(*, allow_unsafe_local: bool = False):
    """Select Docker by default, even when its daemon is unavailable.

    Host execution requires an explicit development-only opt-in from the
    composition root. A missing Docker daemon never selects it automatically.
    """
    if docker_available():
        return DockerSandboxProvider()
    if allow_unsafe_local:
        return LocalProcessSandboxProvider()
    return DockerSandboxProvider()
