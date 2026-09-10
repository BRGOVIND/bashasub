"""Centralised configuration.

Every environment variable the backend reads is declared here. Scattering
``os.environ`` calls through the codebase is how a secret ends up somewhere it
should not be, and how limits end up inconsistent between the component that
enforces them and the component that reports them.

Standard library only, so that configuration can be imported by the domain and
workspace layers without dragging in a web framework.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Limits", "AIConfig", "RedstoneConfig", "load_config", "config"]


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _str_env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@dataclass(frozen=True, slots=True)
class Limits:
    """Resource ceilings. Nothing in Redstone is allowed to be unbounded."""

    max_file_size: int = 2 * 1024 * 1024          # bytes, single file
    max_project_size: int = 200 * 1024 * 1024     # bytes, whole workspace
    max_files: int = 5_000
    max_directory_depth: int = 24
    max_read_size: int = 2 * 1024 * 1024
    max_write_size: int = 2 * 1024 * 1024

    max_agent_iterations: int = 12
    max_agent_timeout: int = 300                  # seconds
    max_build_timeout: int = 300
    max_runtime_timeout: int = 1800
    max_log_size: int = 256 * 1024                # bytes retained per runtime
    max_tool_output: int = 64 * 1024

    @classmethod
    def from_env(cls) -> Limits:
        # slots=True makes cls.<field> a member descriptor rather than the
        # default value, so defaults are read from an instance.
        d = cls()
        return cls(
            max_file_size=_int_env("REDSTONE_MAX_FILE_SIZE", d.max_file_size),
            max_project_size=_int_env("REDSTONE_MAX_PROJECT_SIZE", d.max_project_size),
            max_files=_int_env("REDSTONE_MAX_FILES", d.max_files),
            max_directory_depth=_int_env("REDSTONE_MAX_DEPTH", d.max_directory_depth),
            max_read_size=_int_env("REDSTONE_MAX_READ_SIZE", d.max_read_size),
            max_write_size=_int_env("REDSTONE_MAX_WRITE_SIZE", d.max_write_size),
            max_agent_iterations=_int_env("REDSTONE_MAX_AGENT_ITERATIONS", d.max_agent_iterations),
            max_agent_timeout=_int_env("REDSTONE_MAX_AGENT_TIMEOUT", d.max_agent_timeout),
            max_build_timeout=_int_env("REDSTONE_MAX_BUILD_TIMEOUT", d.max_build_timeout),
            max_runtime_timeout=_int_env("REDSTONE_MAX_RUNTIME_TIMEOUT", d.max_runtime_timeout),
            max_log_size=_int_env("REDSTONE_MAX_LOG_SIZE", d.max_log_size),
            max_tool_output=_int_env("REDSTONE_MAX_TOOL_OUTPUT", d.max_tool_output),
        )


@dataclass(frozen=True, slots=True)
class AIConfig:
    """Provider credentials and selection.

    The key is held here and nowhere else. It is never written to a workspace,
    never placed in a runtime environment, never logged, and never returned by
    an API. ``__repr__`` is overridden because dataclass reprs are printed by
    debuggers, test failures and exception handlers.
    """

    provider: str = "gemini"
    api_key: str = ""
    model: str = "gemini-2.0-flash"
    base_url: str = ""
    timeout_seconds: int = 60
    max_retries: int = 2

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def __repr__(self) -> str:
        return (
            f"AIConfig(provider={self.provider!r}, model={self.model!r}, "
            f"api_key={'<set>' if self.api_key else '<unset>'})"
        )

    __str__ = __repr__

    @classmethod
    def from_env(cls) -> AIConfig:
        d = cls()
        return cls(
            provider=_str_env("AI_PROVIDER", d.provider).lower(),
            api_key=_str_env("AI_API_KEY"),
            model=_str_env("AI_MODEL", d.model),
            base_url=_str_env("AI_BASE_URL"),
            timeout_seconds=_int_env("AI_TIMEOUT_SECONDS", d.timeout_seconds),
            max_retries=_int_env("AI_MAX_RETRIES", d.max_retries),
        )


@dataclass(frozen=True, slots=True)
class RedstoneConfig:
    workspaces_root: Path = field(default_factory=lambda: Path("workspaces").resolve())
    limits: Limits = field(default_factory=Limits)
    ai: AIConfig = field(default_factory=AIConfig)

    def public_health(self) -> dict:
        """Health payload. Reports whether things are configured, never what to."""
        return {
            "status": "ok",
            "ai": {
                "configured": self.ai.is_configured,
                "provider": self.ai.provider,
                "model": self.ai.model,
            },
            "limits": {
                "max_file_size": self.limits.max_file_size,
                "max_project_size": self.limits.max_project_size,
                "max_files": self.limits.max_files,
                "max_agent_iterations": self.limits.max_agent_iterations,
            },
        }


def load_config() -> RedstoneConfig:
    root = _str_env("REDSTONE_WORKSPACES_ROOT")
    return RedstoneConfig(
        workspaces_root=Path(root).resolve() if root else Path("workspaces").resolve(),
        limits=Limits.from_env(),
        ai=AIConfig.from_env(),
    )


config = load_config()
