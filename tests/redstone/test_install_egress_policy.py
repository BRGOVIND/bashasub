"""Phase 4.2 install egress -- UNIT tests (no Docker): the policy object,
operator configuration, and RuntimeManager's choice of network policy.

Enforcement itself is proven only by the REAL DOCKER tests in
test_install_egress.py; nothing here claims network behaviour.
"""

from __future__ import annotations

import pytest

from redstone.config import Limits
from redstone.domain.models import Framework
from redstone.runtime.manager import RuntimeManager
from redstone.sandbox.models import InstallEgressPolicy, NetworkPolicy, validate_registry_host
from redstone.workspace.manager import WorkspaceManager
from runtime_fakes import FakeSandboxProvider


@pytest.mark.parametrize("bad", [
    "1.2.3.4", "127.1", "0x7f.1", "::1", "[::1]", "*.npmjs.org", "https://registry.npmjs.org",
    "registry.npmjs.org:443", "registry.npmjs.org/x", "user@registry.npmjs.org", "", " ",
    "localhost", "a..b.com", "-bad.com", "x" * 300 + ".com", "registry npmjs.org",
])
def test_invalid_registry_hosts_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_registry_host(bad)


def test_registry_hosts_are_normalised():
    assert validate_registry_host("  REGISTRY.NPMJS.ORG. ") == "registry.npmjs.org"


def test_default_policy_is_npm_registry_only_on_443():
    policy = InstallEgressPolicy()
    assert policy.allowed_hosts == ("registry.npmjs.org",)
    assert policy.allowed_ports == (443,)
    assert policy.registry_url == "https://registry.npmjs.org/"


@pytest.mark.parametrize("kwargs", [
    {"allowed_hosts": ()},
    {"allowed_hosts": ("10.0.0.1",)},
    {"allowed_ports": ()},
    {"allowed_ports": (0,)},
    {"allowed_ports": (70000,)},
    {"allowed_ports": (True,)},
    {"max_connections": 0},
    {"max_connections": 100000},
    {"connect_timeout_seconds": 0},
    {"max_tunnel_seconds": 99999},
])
def test_invalid_policies_cannot_be_constructed(kwargs):
    with pytest.raises(ValueError):
        InstallEgressPolicy(**kwargs)


@pytest.mark.parametrize("raw, expected", [
    (None, ("registry.npmjs.org",)),
    ("registry.npmjs.org,npm.pkg.github.com", ("registry.npmjs.org", "npm.pkg.github.com")),
    ("REGISTRY.NPMJS.ORG.", ("registry.npmjs.org",)),
    # All-or-nothing: one bad entry and the restrictive default stands.
    ("registry.npmjs.org,10.0.0.1", ("registry.npmjs.org",)),
    ("*", ("registry.npmjs.org",)),
    ("registry.npmjs.org,*.evil.com", ("registry.npmjs.org",)),
    ("https://evil.com", ("registry.npmjs.org",)),
    (",".join(f"r{i}.example.com" for i in range(9)), ("registry.npmjs.org",)),
])
def test_registry_hosts_env_fails_closed(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("REDSTONE_INSTALL_REGISTRY_HOSTS", raising=False)
    else:
        monkeypatch.setenv("REDSTONE_INSTALL_REGISTRY_HOSTS", raw)
    assert Limits.from_env().install_registry_hosts == expected


@pytest.mark.parametrize("raw, expected", [
    ("false", False), ("0", False), ("no", False), ("true", True), ("1", True),
    ("maybe", True), ("", True),
])
def test_install_network_enabled_env(monkeypatch, raw, expected):
    monkeypatch.setenv("REDSTONE_INSTALL_NETWORK_ENABLED", raw)
    assert Limits.from_env().install_network_enabled is expected


@pytest.mark.parametrize("var, raw, field, expected", [
    ("REDSTONE_INSTALL_PROXY_MAX_CONNECTIONS", "64", "install_proxy_max_connections", 64),
    ("REDSTONE_INSTALL_PROXY_MAX_CONNECTIONS", "0", "install_proxy_max_connections", 32),
    ("REDSTONE_INSTALL_PROXY_MAX_CONNECTIONS", "99999", "install_proxy_max_connections", 32),
    ("REDSTONE_INSTALL_PROXY_CONNECT_TIMEOUT_SECONDS", "5", "install_proxy_connect_timeout_seconds", 5.0),
    ("REDSTONE_INSTALL_PROXY_CONNECT_TIMEOUT_SECONDS", "nan", "install_proxy_connect_timeout_seconds", 10.0),
])
def test_proxy_limits_env_fail_closed(monkeypatch, var, raw, field, expected):
    monkeypatch.setenv(var, raw)
    assert getattr(Limits.from_env(), field) == expected


def _sandbox_configs(tmp_path, **manager_kwargs):
    """Start one runtime whose workspace needs an install, and return the
    configs RuntimeManager asked for: [install sandbox, dev-server sandbox]."""
    provider = FakeSandboxProvider()
    manager = RuntimeManager(provider, max_startup_seconds=2.0, health_poll_interval=0.05,
                             **manager_kwargs)
    workspace = WorkspaceManager(tmp_path / "workspaces").create("ws_policy")
    (workspace.project_root / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    runtime = manager.create("prj_x", workspace, Framework.REACT_VITE_TS)
    manager.start(runtime.id, "prj_x", workspace)
    return [provider.configs[sid] for sid in provider.create_calls]


def test_install_uses_install_only_with_the_configured_policy(tmp_path):
    policy = InstallEgressPolicy(allowed_hosts=("registry.npmjs.org", "npm.pkg.github.com"))
    install, server = _sandbox_configs(tmp_path, egress_policy=policy)
    assert install.network_policy is NetworkPolicy.INSTALL_ONLY
    assert install.egress == policy
    assert server.network_policy is NetworkPolicy.DENY


def test_default_install_policy_is_registry_only(tmp_path):
    install, _ = _sandbox_configs(tmp_path)
    assert install.egress == InstallEgressPolicy()


def test_disabled_install_network_means_no_network_never_more(tmp_path):
    configs = _sandbox_configs(tmp_path, install_network_enabled=False)
    assert len(configs) == 2
    assert all(c.network_policy is NetworkPolicy.DENY for c in configs)
