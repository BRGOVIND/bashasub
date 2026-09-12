"""SandboxValidationRunner: the concrete ValidationRunner the Phase 3 seam
(redstone.agent.tools.validation.ValidationRunner) was built for.

Fake-provider tests cover the PASSED/FAILED/TIMEOUT/UNAVAILABLE mapping and
that a sandbox is always torn down, even on failure. The real-Docker test
proves the same class against an actual container using the same minimal
fixture project as test_runtime_docker_integration.py.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from redstone.agent.tools.validation import ValidationStatus
from redstone.domain.models import Framework
from redstone.runtime.validation import SandboxValidationRunner
from redstone.sandbox.errors import RedstoneSandboxError, SandboxErrorCode
from redstone.sandbox.models import SandboxResult
from redstone.sandbox.providers.docker_provider import DockerSandboxProvider, docker_available
from runtime_fakes import FakeSandboxProvider

DOCKER_UP = docker_available()
skip_no_docker = pytest.mark.skipif(not DOCKER_UP, reason="Docker daemon not reachable")

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "react_vite_ts_min"


def _project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for item in FIXTURE_DIR.iterdir():
        shutil.copy(item, root / item.name)
    return root


# ------------------------------------------------------------ fake provider

def test_build_passes_when_the_sandbox_exits_zero(tmp_path):
    provider = FakeSandboxProvider(wait_result=SandboxResult(
        exit_code=0, stdout="build-ok", stderr="", truncated=False,
        timed_out=False, duration_seconds=0.1,
    ))
    runner = SandboxValidationRunner(provider)

    result = runner.run_build(_project(tmp_path))

    assert result.status == ValidationStatus.PASSED
    assert "build-ok" in result.output
    assert not provider.live_sandbox_ids()   # always torn down


def test_typecheck_fails_when_the_sandbox_exits_nonzero(tmp_path):
    provider = FakeSandboxProvider(wait_result=SandboxResult(
        exit_code=2, stdout="", stderr="src/App.tsx:1:1 - error TS1005",
        truncated=False, timed_out=False, duration_seconds=0.2,
    ))
    runner = SandboxValidationRunner(provider)

    result = runner.run_typecheck(_project(tmp_path))

    assert result.status == ValidationStatus.FAILED
    assert "TS1005" in result.output
    assert not provider.live_sandbox_ids()


def test_lint_reports_failed_on_timeout_not_a_fabricated_pass(tmp_path):
    provider = FakeSandboxProvider(wait_result=SandboxResult(
        exit_code=None, stdout="", stderr="", truncated=False,
        timed_out=True, duration_seconds=60.0,
    ))
    runner = SandboxValidationRunner(provider)

    result = runner.run_lint(_project(tmp_path))

    assert result.status == ValidationStatus.FAILED
    assert "timed out" in result.message


def test_provider_unavailable_is_reported_not_raised(tmp_path):
    provider = FakeSandboxProvider(
        create_error=RedstoneSandboxError(SandboxErrorCode.PROVIDER_UNAVAILABLE)
    )
    runner = SandboxValidationRunner(provider)

    result = runner.run_build(_project(tmp_path))

    assert result.status == ValidationStatus.UNAVAILABLE


def test_never_reads_dependencies_or_installs_them(tmp_path):
    """SandboxValidationRunner must not itself run an install phase -- that
    would duplicate RuntimeManager's own install/network handling."""
    provider = FakeSandboxProvider()
    runner = SandboxValidationRunner(provider)

    runner.run_build(_project(tmp_path))

    assert len(provider.create_calls) == 1   # exactly one sandbox: the build itself


def test_validation_sandbox_gets_no_network(tmp_path):
    captured = {}
    provider = FakeSandboxProvider()
    original_create = provider.create

    def _spy_create(config):
        captured["network_policy"] = config.network_policy
        return original_create(config)

    provider.create = _spy_create
    runner = SandboxValidationRunner(provider)

    runner.run_typecheck(_project(tmp_path))

    from redstone.sandbox.models import NetworkPolicy
    assert captured["network_policy"] is NetworkPolicy.DENY


# ------------------------------------------------------------- real docker

@skip_no_docker
def test_build_succeeds_against_a_real_container(tmp_path):
    provider = DockerSandboxProvider()
    runner = SandboxValidationRunner(provider, framework=Framework.REACT_VITE_TS)

    result = runner.run_build(_project(tmp_path))

    assert result.status == ValidationStatus.PASSED
    assert "build-ok" in result.output


@skip_no_docker
def test_typecheck_fails_honestly_when_tsc_is_not_installed(tmp_path):
    """The fixture project has no devDependencies at all -- `npx --no-install
    tsc` must fail for real, not be silently reported as passing."""
    provider = DockerSandboxProvider()
    runner = SandboxValidationRunner(provider, framework=Framework.REACT_VITE_TS)

    result = runner.run_typecheck(_project(tmp_path))

    assert result.status == ValidationStatus.FAILED
