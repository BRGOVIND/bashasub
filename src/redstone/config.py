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


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    """Operator override for a sandbox ceiling. Fails closed: anything
    unparsable or outside [minimum, maximum] yields the safe default rather
    than being clamped or honoured -- so a mistyped or malicious value can
    never widen (or disable) a limit."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


def _bool_env(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    return default


def _registry_hosts_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Comma-separated install registry hosts. All-or-nothing: if any entry
    is invalid (an IP, a wildcard, a URL, ...) the whole value is ignored and
    the restrictive default stands -- a typo must never widen egress, and a
    partially-applied list would be a surprise either way."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    from .sandbox.models import validate_registry_host   # lazy: config stays import-light
    entries = [part for part in raw.split(",") if part.strip()]
    if not entries or len(entries) > 8:
        return default
    try:
        return tuple(dict.fromkeys(validate_registry_host(part) for part in entries))
    except ValueError:
        return default


def _bounded_float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    if value != value or value in (float("inf"), float("-inf")):   # NaN / inf
        return default
    return value if minimum <= value <= maximum else default


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

    max_snapshots_per_workspace: int = 20
    max_snapshot_storage: int = 500 * 1024 * 1024  # bytes across all snapshots

    max_agent_context_bytes: int = 200 * 1024      # total conversation sent to the model
    max_tool_argument_size: int = 64 * 1024        # a single string tool argument
    max_search_results: int = 50                   # agent-facing cap, below the raw tool's own

    # Sandbox / runtime (Phase 4/5; storage + env overrides Phase 4.1).
    sandbox_cpu_cores: float = 1.0
    sandbox_memory_mb: int = 512
    sandbox_pids: int = 128
    sandbox_output_bytes: int = 256 * 1024
    sandbox_storage_mb: int = 1024
    max_runtime_startup_seconds: float = 60.0      # install + start + first healthy check
    runtime_health_check_timeout_seconds: float = 5.0
    runtime_stop_grace_seconds: float = 10.0

    # Install egress (Phase 4.2). Disabling install networking makes
    # installs run with no network at all -- never with more.
    install_network_enabled: bool = True
    install_registry_hosts: tuple[str, ...] = ("registry.npmjs.org",)
    install_proxy_connect_timeout_seconds: float = 10.0
    install_proxy_max_connections: int = 32

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
            max_snapshots_per_workspace=_int_env(
                "REDSTONE_MAX_SNAPSHOTS", d.max_snapshots_per_workspace
            ),
            max_snapshot_storage=_int_env(
                "REDSTONE_MAX_SNAPSHOT_STORAGE", d.max_snapshot_storage
            ),
            max_agent_context_bytes=_int_env(
                "REDSTONE_MAX_AGENT_CONTEXT", d.max_agent_context_bytes
            ),
            max_tool_argument_size=_int_env(
                "REDSTONE_MAX_TOOL_ARGUMENT_SIZE", d.max_tool_argument_size
            ),
            max_search_results=_int_env(
                "REDSTONE_MAX_SEARCH_RESULTS", d.max_search_results
            ),
            # Sandbox ceilings. Bounds are deliberately finite on BOTH sides:
            # there is no value that means "unlimited", and no value that
            # turns a limit off. Out-of-range input falls back to the default.
            sandbox_cpu_cores=_bounded_float_env(
                "REDSTONE_SANDBOX_CPU_CORES", d.sandbox_cpu_cores, 0.1, 8.0
            ),
            sandbox_memory_mb=_bounded_int_env(
                "REDSTONE_SANDBOX_MEMORY_MB", d.sandbox_memory_mb, 64, 8192
            ),
            sandbox_pids=_bounded_int_env(
                "REDSTONE_SANDBOX_PIDS", d.sandbox_pids, 16, 1024
            ),
            sandbox_output_bytes=_bounded_int_env(
                "REDSTONE_SANDBOX_OUTPUT_BYTES", d.sandbox_output_bytes, 4 * 1024, 4 * 1024 * 1024
            ),
            sandbox_storage_mb=_bounded_int_env(
                "REDSTONE_SANDBOX_STORAGE_MB", d.sandbox_storage_mb, 16, 16 * 1024
            ),
            max_runtime_startup_seconds=_bounded_float_env(
                "REDSTONE_RUNTIME_STARTUP_SECONDS", d.max_runtime_startup_seconds, 5.0, 600.0
            ),
            runtime_health_check_timeout_seconds=_bounded_float_env(
                "REDSTONE_RUNTIME_HEALTH_TIMEOUT_SECONDS",
                d.runtime_health_check_timeout_seconds, 1.0, 60.0,
            ),
            runtime_stop_grace_seconds=_bounded_float_env(
                "REDSTONE_RUNTIME_STOP_GRACE_SECONDS", d.runtime_stop_grace_seconds, 1.0, 120.0
            ),
            install_network_enabled=_bool_env(
                "REDSTONE_INSTALL_NETWORK_ENABLED", d.install_network_enabled
            ),
            install_registry_hosts=_registry_hosts_env(
                "REDSTONE_INSTALL_REGISTRY_HOSTS", d.install_registry_hosts
            ),
            install_proxy_connect_timeout_seconds=_bounded_float_env(
                "REDSTONE_INSTALL_PROXY_CONNECT_TIMEOUT_SECONDS",
                d.install_proxy_connect_timeout_seconds, 1.0, 60.0,
            ),
            install_proxy_max_connections=_bounded_int_env(
                "REDSTONE_INSTALL_PROXY_MAX_CONNECTIONS", d.install_proxy_max_connections, 1, 256
            ),
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
    retry_backoff_seconds: float = 0.5           # base for exponential backoff
    max_request_bytes: int = 1 * 1024 * 1024     # prompt payload ceiling
    max_response_bytes: int = 8 * 1024 * 1024    # provider response ceiling

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
            timeout_seconds=_int_env("AI_REQUEST_TIMEOUT", d.timeout_seconds),
            max_retries=_int_env("AI_MAX_RETRIES", d.max_retries),
            max_request_bytes=_int_env("MAX_AI_REQUEST_SIZE", d.max_request_bytes),
            max_response_bytes=_int_env("MAX_AI_RESPONSE_SIZE", d.max_response_bytes),
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
