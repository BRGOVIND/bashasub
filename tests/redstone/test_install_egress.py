"""Phase 4.2 install egress -- REAL DOCKER (skipped, with an explicit reason,
when no daemon is reachable). Nothing here mocks the network.

Threat model: arbitrary code execution inside `npm install`. Every test
below runs that "attacker" as a real process inside a real INSTALL_ONLY
sandbox and observes what the network actually lets it do.

Topology under test (see docs/redstone/SANDBOX.md):

    install sandbox --(<id>-net, Docker --internal)--> <id>-proxy --(<id>-egress)--> internet

The sandbox has no route anywhere except the proxy, and the proxy allows
`CONNECT <allowlisted host>:443` to public addresses only.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import time
import uuid

import pytest

from redstone.domain.models import Framework
from redstone.runtime.manager import (
    PROJECT_LABEL,
    RUNTIME_LABEL,
    WORKSPACE_LABEL,
    RuntimeManager,
)
from redstone.sandbox.commands import Operation, SandboxCommand
from redstone.sandbox.errors import RedstoneSandboxError, SandboxErrorCode
from redstone.sandbox.models import (
    InstallEgressPolicy,
    Mount,
    NetworkPolicy,
    ResourceLimits,
    SandboxConfig,
)
from redstone.sandbox.providers.docker_provider import (
    DEFAULT_IMAGE,
    MANAGED_LABEL,
    PROXY_SCRIPT,
    DockerSandboxProvider,
    docker_available,
)

pytestmark = pytest.mark.skipif(not docker_available(), reason="Docker daemon not reachable")

REGISTRY = "registry.npmjs.org"
NPM_FAST_FAIL = ("--fetch-retries=0", "--fetch-timeout=8000")


class _FixedCommand:
    def __init__(self, argv):
        self._argv = tuple(argv)

    def resolve(self):
        return self._argv


def _docker(*args, check=False, timeout=60):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                          check=check)


def _managed_names() -> tuple[set[str], set[str]]:
    containers = set(_docker("ps", "-a", "--filter", f"label={MANAGED_LABEL}=true",
                             "--format", "{{.Names}}").stdout.split())
    networks = set(_docker("network", "ls", "--filter", f"label={MANAGED_LABEL}=true",
                           "--format", "{{.Name}}").stdout.split())
    return containers, networks


@pytest.fixture
def docker():
    provider = DockerSandboxProvider()
    created: list[str] = []
    real_create = provider.create

    def tracking_create(config):
        sandbox_id = real_create(config)
        created.append(sandbox_id)
        return sandbox_id

    provider.create = tracking_create
    yield provider
    for sandbox_id in created:
        try:
            provider.destroy(sandbox_id)
        except RedstoneSandboxError:
            pass


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "ws" / "project"
    root.mkdir(parents=True)
    return root


def _config(project, argv=("sleep", "300"), *, policy=None, labels=None, env=None, limits=None):
    return SandboxConfig(
        mounts=(Mount(project, "/workspace/project", read_only=False),),
        working_dir="/workspace/project",
        command=argv if hasattr(argv, "resolve") else _FixedCommand(argv),
        network_policy=NetworkPolicy.INSTALL_ONLY,
        egress=policy or InstallEgressPolicy(),
        labels=labels or {},
        environment=env or {},
        resource_limits=limits or ResourceLimits(),
    )


def _install_sandbox(docker, project, **kwargs):
    sandbox_id = docker.create(_config(project, **kwargs))
    docker.start(sandbox_id)
    return sandbox_id


def _exec(docker, sandbox_id, *argv, timeout=60):
    return docker.exec_in(sandbox_id, tuple(argv), timeout=timeout)


def _ip_on(container: str, network: str) -> str:
    return _docker("inspect", "-f",
                   "{{(index .NetworkSettings.Networks \"" + network + "\").IPAddress}}",
                   container).stdout.strip()


# Single-line node probes (passed with `node -e`).
CONNECT_PROBE = (
    "const t=process.argv[1];const u=new URL(process.env.HTTPS_PROXY);"
    "const s=require('net').connect(Number(u.port),u.hostname);"
    "s.on('connect',()=>s.write('CONNECT '+t+' HTTP/1.1'+String.fromCharCode(13,10)"
    "+'Host: '+t+String.fromCharCode(13,10,13,10)));"
    "s.setTimeout(10000,()=>{console.log('TIMEOUT');process.exit(2)});"
    "s.on('data',d=>{console.log(String(d).split(String.fromCharCode(13))[0]);process.exit(0)});"
    "s.on('error',e=>{console.log('ERR '+e.code);process.exit(1)})"
)
RAW_REQUEST_PROBE = (
    "const line=process.argv[1];const u=new URL(process.env.HTTPS_PROXY);"
    "const s=require('net').connect(Number(u.port),u.hostname);"
    "s.on('connect',()=>s.write(line+String.fromCharCode(13,10)+'Host: x'+String.fromCharCode(13,10,13,10)));"
    "s.setTimeout(10000,()=>{console.log('TIMEOUT');process.exit(2)});"
    "s.on('data',d=>{console.log(String(d).split(String.fromCharCode(13))[0]);process.exit(0)});"
    "s.on('error',e=>{console.log('ERR '+e.code);process.exit(1)})"
)
DIRECT_PROBE = (
    "const [h,p]=process.argv.slice(1);const s=require('net').connect(Number(p),h);"
    "s.setTimeout(4000,()=>{console.log('TIMEOUT');process.exit(2)});"
    "s.on('connect',()=>{console.log('CONNECTED');process.exit(0)});"
    "s.on('error',e=>{console.log(e.code);process.exit(1)})"
)


def _connect_status(docker, sandbox_id, target) -> str:
    return _exec(docker, sandbox_id, "node", "-e", CONNECT_PROBE, target, timeout=30).stdout.strip()


def _direct(docker, sandbox_id, host, port) -> str:
    return _exec(docker, sandbox_id, "node", "-e", DIRECT_PROBE, host, str(port), timeout=20).stdout.strip()


def _proxy_decisions(sandbox_id) -> list[dict]:
    lines = _docker("logs", f"{sandbox_id}-proxy").stdout.splitlines()
    return [json.loads(line) for line in lines if line.startswith("{")]


# ===================================================== A. allowed registry

def test_npm_reaches_the_allowed_registry_through_the_proxy(docker, project):
    sandbox_id = _install_sandbox(docker, project)
    result = _exec(docker, sandbox_id, "npm", "view", "is-odd", "version", *NPM_FAST_FAIL, timeout=90)
    assert result.exit_code == 0, result.stdout + result.stderr
    assert re.match(r"^\d+\.\d+\.\d+", result.stdout.strip())

    allows = [d for d in _proxy_decisions(sandbox_id) if d.get("decision") == "allow"]
    assert allows and {d["host"] for d in allows} == {REGISTRY}


def test_real_npm_install_uses_only_the_registry(docker, project, capsys):
    """What does npm ACTUALLY need? A real install with a transitive
    dependency, then the proxy's own decision log is the evidence: every
    connection npm made went to registry.npmjs.org -- metadata and tarballs
    alike -- so no other host needs allowing."""
    (project / "package.json").write_text(
        json.dumps({"name": "egress-probe", "version": "1.0.0", "private": True,
                    "dependencies": {"is-odd": "3.0.1"}}), encoding="utf-8")
    command = SandboxCommand(Operation.INSTALL_DEPENDENCIES, Framework.REACT_VITE_TS)

    started = time.monotonic()
    sandbox_id = docker.create(_config(project, command, limits=ResourceLimits(timeout_seconds=300)))
    ready = time.monotonic()
    docker.start(sandbox_id)
    result = docker.wait(sandbox_id, timeout=300)
    finished = time.monotonic()
    decisions = _proxy_decisions(sandbox_id)
    docker.destroy(sandbox_id)
    cleaned = time.monotonic()

    assert result.ok, result.stdout[-2000:] + result.stderr[-2000:]
    assert (project / "node_modules" / "is-odd" / "package.json").is_file()
    assert (project / "node_modules" / "is-number" / "package.json").is_file()   # transitive
    hosts = {d["host"] for d in decisions if d.get("decision") == "allow"}
    denied = [d for d in decisions if d.get("decision") == "deny"]
    assert hosts == {REGISTRY}
    assert denied == []
    with capsys.disabled():
        print(f"\n[install-egress timing] proxy+sandbox create {ready - started:.2f}s, "
              f"install {finished - ready:.2f}s, cleanup {cleaned - finished:.2f}s, "
              f"tunnels {sum(1 for d in decisions if d.get('decision') == 'allow')}")


# ============================================= B-H, M. CONNECT policy matrix

def _resolved_registry_ip() -> str:
    return next(a[4][0] for a in socket.getaddrinfo(REGISTRY, 443, socket.AF_INET))


@pytest.mark.parametrize("target, reason", [
    ("example.com:443", "arbitrary internet"),
    ("google.com:443", "google"),
    ("www.google.com:443", "google"),
    ("github.com:443", "not required by npm"),
    ("raw.githubusercontent.com:443", "not required by npm"),
    ("registry.yarnpkg.com:443", "unauthorized registry"),
    ("1.1.1.1:443", "arbitrary public IP"),
    ("8.8.8.8:53", "arbitrary public IP/port"),
    ("10.0.0.1:443", "RFC1918"),
    ("172.16.0.1:443", "RFC1918"),
    ("192.168.1.1:443", "RFC1918"),
    ("127.0.0.1:443", "loopback"),
    ("localhost:443", "loopback name"),
    ("[::1]:443", "IPv6 loopback"),
    ("169.254.169.254:80", "cloud metadata"),
    ("169.254.1.1:443", "link-local"),
    ("172.17.0.1:443", "Docker gateway"),
    ("registry.npmjs.org:80", "allowed host, disallowed port"),
    ("registry.npmjs.org.evil.example:443", "suffix trick"),
    ("www.registry.npmjs.org:443", "subdomain is not the host"),
    ("registry.npmjs.org@evil.example:443", "userinfo trick"),
    ("0x7f.1:443", "numeric name"),
])
def test_proxy_denies_everything_but_the_allowlist(docker, project, target, reason):
    sandbox_id = _install_sandbox(docker, project)
    status = _connect_status(docker, sandbox_id, target)
    assert status.startswith("HTTP/1.1 403"), f"{reason}: {status}"


def test_direct_ip_of_the_allowed_registry_is_denied(docker, project):
    """The registry's own address, not its name: denied, so the hostname
    policy can't be sidestepped by resolving it yourself."""
    sandbox_id = _install_sandbox(docker, project)
    ip = _resolved_registry_ip()
    assert _connect_status(docker, sandbox_id, f"{ip}:443").startswith("HTTP/1.1 403")
    assert _direct(docker, sandbox_id, ip, 443) != "CONNECTED"


@pytest.mark.parametrize("target", ["registry.npmjs.org:443", "REGISTRY.NPMJS.ORG.:443"])
def test_proxy_allows_the_registry_in_canonical_forms(docker, project, target):
    sandbox_id = _install_sandbox(docker, project)
    assert _connect_status(docker, sandbox_id, target).startswith("HTTP/1.1 200")


@pytest.mark.parametrize("line", [
    "GET http://registry.npmjs.org/ HTTP/1.1",   # absolute-URI forward proxying
    "GET / HTTP/1.1",
    "POST http://example.com/ HTTP/1.1",
])
def test_proxy_never_forwards_or_follows_plain_http(docker, project, line):
    """N (redirects): the proxy only splices CONNECT tunnels. It never makes a
    request itself, so it never follows a redirect -- a redirect to another
    host costs the client a NEW CONNECT, which the policy judges on its own
    (see the matrix above: every non-allowlisted host is 403)."""
    sandbox_id = _install_sandbox(docker, project)
    out = _exec(docker, sandbox_id, "node", "-e", RAW_REQUEST_PROBE, line, timeout=30).stdout.strip()
    assert out.startswith("HTTP/1.1 405"), out


# ===================================================== direct paths / DNS

@pytest.mark.parametrize("host, port", [
    ("1.1.1.1", 443), ("8.8.8.8", 53), ("10.0.0.1", 443), ("192.168.1.1", 443),
    ("172.17.0.1", 443), ("169.254.169.254", 80),
])
def test_sandbox_has_no_direct_route(docker, project, host, port):
    sandbox_id = _install_sandbox(docker, project)
    outcome = _direct(docker, sandbox_id, host, port)
    assert outcome in ("ENETUNREACH", "EHOSTUNREACH"), outcome


def test_sandbox_loopback_is_its_own_and_empty(docker, project):
    sandbox_id = _install_sandbox(docker, project)
    assert _direct(docker, sandbox_id, "127.0.0.1", 443) == "ECONNREFUSED"


def test_sandbox_cannot_resolve_external_names(docker, project):
    """No external DNS on the --internal network: DNS is neither a route nor
    a data-exfiltration side channel."""
    sandbox_id = _install_sandbox(docker, project)
    out = _exec(docker, sandbox_id, "node", "-e",
                "require('dns').lookup(process.argv[1],(e,a)=>console.log(e?e.code:'RESOLVED '+a))",
                "example.com").stdout.strip()
    assert out in ("EAI_AGAIN", "ENOTFOUND"), out


# ================================================ J-L. proxy bypass attempts

PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
              "npm_config_proxy", "npm_config_https_proxy")


def test_removing_proxy_settings_leaves_no_network(docker, project):
    sandbox_id = _install_sandbox(docker, project)
    unset = [flag for var in PROXY_VARS for flag in ("-u", var)]
    result = _exec(docker, sandbox_id, "env", *unset, "npm", "view", "is-odd", "version",
                   *NPM_FAST_FAIL, timeout=90)
    assert result.exit_code != 0


def test_no_proxy_star_cannot_reach_anything_else(docker, project):
    """NO_PROXY=* cannot open a direct path because there is none. (npm keeps
    honouring its explicit https-proxy setting, so even its allowed traffic
    still goes through the policy -- verified in the proxy log.)"""
    sandbox_id = _install_sandbox(docker, project)
    other = _exec(docker, sandbox_id, "env", "NO_PROXY=*", "no_proxy=*", "npm", "view", "is-odd",
                  "--registry=https://registry.yarnpkg.com/", *NPM_FAST_FAIL, timeout=90)
    assert other.exit_code != 0
    direct = _exec(docker, sandbox_id, "env", "NO_PROXY=*", "no_proxy=*",
                   "node", "-e", DIRECT_PROBE, "1.1.1.1", "443", timeout=20)
    assert direct.stdout.strip() != "CONNECTED"


def test_attacker_proxy_is_unreachable(docker, project):
    sandbox_id = _install_sandbox(docker, project)
    evil = "http://1.1.1.1:8080"
    result = _exec(docker, sandbox_id, "env", *[f"{v}={evil}" for v in PROXY_VARS],
                   "npm", "view", "is-odd", "version", *NPM_FAST_FAIL, timeout=90)
    assert result.exit_code != 0


def test_registry_override_is_refused(docker, project):
    """Both the env/CLI route and a project .npmrc -- the one a malicious
    repository controls."""
    sandbox_id = _install_sandbox(docker, project)
    cli = _exec(docker, sandbox_id, "npm", "view", "is-odd",
                "--registry=https://registry.yarnpkg.com/", *NPM_FAST_FAIL, timeout=90)
    assert cli.exit_code != 0

    (project / ".npmrc").write_text("registry=https://registry.yarnpkg.com/\n", encoding="utf-8")
    npmrc = _exec(docker, sandbox_id, "env", "-u", "npm_config_registry",
                  "npm", "view", "is-odd", "version", *NPM_FAST_FAIL, timeout=90)
    assert npmrc.exit_code != 0
    denied = {d.get("host") for d in _proxy_decisions(sandbox_id) if d.get("decision") == "deny"}
    assert "registry.yarnpkg.com" in denied


# ==================================== I, 15. other containers / workspaces

@pytest.fixture
def unrelated_container():
    name = f"rs-egress-target-{uuid.uuid4().hex[:8]}"
    _docker("run", "-d", "--name", name, "alpine:3.19", "sh", "-c",
            "while true; do echo hi | nc -l -p 8080; done", check=True)
    time.sleep(1)
    ip = _docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                 name).stdout.strip()
    try:
        yield ip
    finally:
        _docker("rm", "-f", name)


def test_two_workspaces_are_isolated_from_each_other(docker, tmp_path, unrelated_container):
    a_dir = tmp_path / "a" / "project"
    b_dir = tmp_path / "b" / "project"
    a_dir.mkdir(parents=True)
    b_dir.mkdir(parents=True)
    a = _install_sandbox(docker, a_dir)
    b = _install_sandbox(docker, b_dir)

    b_ip = _ip_on(b, f"{b}-net")
    b_proxy_ip = _ip_on(f"{b}-proxy", f"{b}-net")
    a_proxy_ip = _ip_on(f"{a}-proxy", f"{a}-net")
    assert b_ip and b_proxy_ip and a_proxy_ip

    # A cannot reach B's sandbox, B's proxy, or an unrelated container ...
    for host, port in ((b_ip, 8080), (b_proxy_ip, 3128), (unrelated_container, 8080)):
        assert _direct(docker, a, host, port) != "CONNECTED", (host, port)
        assert _connect_status(docker, a, f"{host}:{port}").startswith("HTTP/1.1 403")
    # ... and its own proxy exposes nothing but the proxy port.
    assert _direct(docker, a, a_proxy_ip, 22) != "CONNECTED"

    # Destroying A leaves B's infrastructure intact and working.
    docker.destroy(a)
    _, networks = _managed_names()
    assert f"{b}-net" in networks and f"{b}-egress" in networks
    assert f"{a}-net" not in networks and f"{a}-egress" not in networks
    assert _connect_status(docker, b, f"{REGISTRY}:443").startswith("HTTP/1.1 200")


# ====================================================== 13. fail closed

def _sandbox_networks(sandbox_id) -> set[str]:
    raw = _docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", sandbox_id).stdout
    return set(json.loads(raw))


def test_stopped_proxy_means_no_network_not_a_fallback(docker, project):
    sandbox_id = _install_sandbox(docker, project)
    assert _connect_status(docker, sandbox_id, f"{REGISTRY}:443").startswith("HTTP/1.1 200")

    _docker("stop", "-t", "1", f"{sandbox_id}-proxy", check=True)

    result = _exec(docker, sandbox_id, "npm", "view", "is-odd", "version", *NPM_FAST_FAIL, timeout=90)
    assert result.exit_code != 0
    assert _direct(docker, sandbox_id, _resolved_registry_ip(), 443) != "CONNECTED"
    assert _sandbox_networks(sandbox_id) == {f"{sandbox_id}-net"}


@pytest.mark.parametrize("script", [
    "process.exit(1)",                        # proxy crashes at startup
    "setInterval(() => {}, 1000)",            # proxy never becomes ready
    None,                                     # proxy script missing
])
def test_unavailable_proxy_fails_create_closed(tmp_path, script):
    before = _managed_names()
    fake = tmp_path / "proxy.js"
    if script is not None:
        fake.write_text(script, encoding="utf-8")
    provider = DockerSandboxProvider(egress_proxy_script=fake)
    project = tmp_path / "project"
    project.mkdir()
    policy = InstallEgressPolicy(startup_timeout_seconds=3)

    with pytest.raises(RedstoneSandboxError) as caught:
        provider.create(_config(project, policy=policy))

    assert caught.value.code is SandboxErrorCode.CREATE_FAILED
    assert _managed_names() == before, "a failed proxy must leave nothing behind"


def test_misconfigured_allowlist_makes_the_proxy_refuse_to_start():
    """The proxy itself refuses an empty or numeric allowlist instead of
    running with an unintended policy."""
    for allow in ("", "127.1", "1.2.3.4", "*.npmjs.org"):
        result = _docker(
            "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--user", "1000:1000", "-e", f"REDSTONE_EGRESS_ALLOW={allow}",
            "--mount", f"type=bind,source={PROXY_SCRIPT},target=/p.js,readonly",
            DEFAULT_IMAGE, "node", "/p.js", timeout=60,
        )
        assert result.returncode == 2, (allow, result.stdout, result.stderr)
        assert "READY" not in result.stdout


# ===================================================== hardening / topology

def _inspect(name) -> dict:
    return json.loads(_docker("inspect", name, check=True).stdout)[0]


def test_proxy_and_sandbox_topology_and_hardening(docker, project):
    sandbox_id = _install_sandbox(docker, project)
    proxy = _inspect(f"{sandbox_id}-proxy")
    sandbox = _inspect(sandbox_id)

    for container in (proxy, sandbox):
        host = container["HostConfig"]
        assert host["Privileged"] is False
        assert "ALL" in (host["CapDrop"] or [])
        assert not host.get("CapAdd")
        assert "no-new-privileges" in (host["SecurityOpt"] or [])
        assert host["ReadonlyRootfs"] is True
        assert not host.get("PortBindings")
        assert not host.get("Devices")
        assert container["Config"]["User"] == "1000:1000"
        assert all("docker.sock" not in (m.get("Source") or "") for m in container["Mounts"])

    assert set(sandbox["NetworkSettings"]["Networks"]) == {f"{sandbox_id}-net"}
    assert set(proxy["NetworkSettings"]["Networks"]) == {f"{sandbox_id}-net", f"{sandbox_id}-egress"}
    proxy_mounts = proxy["Mounts"]
    assert len(proxy_mounts) == 1 and proxy_mounts[0]["RW"] is False
    assert proxy["Config"]["Labels"].get("redstone.role") == "egress-proxy"

    internal = json.loads(_docker("network", "inspect", f"{sandbox_id}-net").stdout)[0]
    egress = json.loads(_docker("network", "inspect", f"{sandbox_id}-egress").stdout)[0]
    assert internal["Internal"] is True
    assert egress["Internal"] is False
    assert egress["Options"].get("com.docker.network.bridge.enable_icc") == "false"
    # Exactly two members on the internal network: the sandbox and its proxy.
    assert set(c["Name"] for c in internal["Containers"].values()) == {sandbox_id, f"{sandbox_id}-proxy"}
    assert set(c["Name"] for c in egress["Containers"].values()) == {f"{sandbox_id}-proxy"}


def test_proxy_logs_carry_no_request_headers(docker, project):
    sandbox_id = _install_sandbox(docker, project)
    secret = f"canary-{uuid.uuid4().hex}"
    probe = CONNECT_PROBE.replace("'Host: '+t", f"'Proxy-Authorization: Basic {secret}'")
    _exec(docker, sandbox_id, "node", "-e", probe, f"{REGISTRY}:443", timeout=30)
    _exec(docker, sandbox_id, "node", "-e", probe, "example.com:443", timeout=30)
    assert secret not in _docker("logs", f"{sandbox_id}-proxy").stdout


# ===================================================== cleanup / orphans

def test_destroy_removes_sandbox_proxy_and_both_networks(docker, project):
    sandbox_id = _install_sandbox(docker, project)
    containers, networks = _managed_names()
    assert {sandbox_id, f"{sandbox_id}-proxy"} <= containers
    assert {f"{sandbox_id}-net", f"{sandbox_id}-egress"} <= networks

    docker.destroy(sandbox_id)
    docker.destroy(sandbox_id)   # idempotent

    containers, networks = _managed_names()
    assert not {sandbox_id, f"{sandbox_id}-proxy"} & containers
    assert not {f"{sandbox_id}-net", f"{sandbox_id}-egress"} & networks


def test_orphan_recovery_removes_install_infrastructure(tmp_path, unrelated_container):
    """A Redstone process dies mid-install: sandbox, proxy and both networks
    survive it. A fresh RuntimeManager's reconciliation removes all four --
    and nothing else."""
    project = tmp_path / "project"
    project.mkdir()
    provider = DockerSandboxProvider()
    labels = {PROJECT_LABEL: "prj_dead", WORKSPACE_LABEL: "ws_dead", RUNTIME_LABEL: "rt_deadprocess"}
    sandbox_id = provider.create(_config(project, labels=labels))
    provider.start(sandbox_id)

    report = RuntimeManager(DockerSandboxProvider()).reconcile_orphaned_containers()

    assert sandbox_id in report["destroyed"]
    assert f"{sandbox_id}-proxy" in report["destroyed"]
    containers, networks = _managed_names()
    assert not {sandbox_id, f"{sandbox_id}-proxy"} & containers
    assert not {f"{sandbox_id}-net", f"{sandbox_id}-egress"} & networks
    target = _docker("ps", "--filter", "status=running", "--format", "{{.Names}}").stdout
    assert "rs-egress-target-" in target   # the unrelated container is untouched


# ============================================ 7. DNS policy on the real proxy

DNS_CASES = {
    # allowlisted name -> what it resolves to (via --add-host) -> expected
    "public.test": (["104.16.4.34"], "HTTP/1.1 200"),
    "private.test": (["10.0.0.5"], "HTTP/1.1 403"),
    "cgnat.test": (["100.64.1.1"], "HTTP/1.1 403"),
    "loop.test": (["127.0.0.1"], "HTTP/1.1 403"),
    "linklocal.test": (["169.254.1.1"], "HTTP/1.1 403"),
    "metadata.test": (["169.254.169.254"], "HTTP/1.1 403"),
    "v6loop.test": (["::1"], "HTTP/1.1 403"),
    "v6ula.test": (["fd00::1"], "HTTP/1.1 403"),
    "mapped.test": (["::ffff:10.0.0.5"], "HTTP/1.1 403"),
    "mixed.test": (["104.16.4.34", "10.0.0.5"], "HTTP/1.1 403"),   # one bad answer denies all
}


@pytest.fixture
def standalone_proxy():
    """The real egress_proxy.js, with names pinned by --add-host so each
    resolution case is deterministic -- no public DNS service involved. It
    runs on the default bridge so the one allowed case can really connect."""
    name = f"rs-egress-dns-{uuid.uuid4().hex[:8]}"
    args = ["run", "-d", "--name", name, "--read-only", "--cap-drop", "ALL", "--user", "1000:1000",
            "--security-opt", "no-new-privileges",
            "-e", "REDSTONE_EGRESS_ALLOW=" + ",".join(DNS_CASES),
            "--mount", f"type=bind,source={PROXY_SCRIPT},target=/p.js,readonly"]
    for host, (addresses, _) in DNS_CASES.items():
        for address in addresses:
            args += ["--add-host", f"{host}:{address}"]
    _docker(*args, DEFAULT_IMAGE, "node", "/p.js", check=True)
    deadline = time.monotonic() + 20
    while "READY" not in _docker("logs", name).stdout:
        assert time.monotonic() < deadline, _docker("logs", name).stdout
        time.sleep(0.2)
    try:
        yield name
    finally:
        _docker("rm", "-f", name)


LOCAL_CONNECT = CONNECT_PROBE.replace(
    "const u=new URL(process.env.HTTPS_PROXY);", "const u={port:'3128',hostname:'127.0.0.1'};"
)


@pytest.mark.parametrize("host", list(DNS_CASES))
def test_proxy_judges_what_a_name_resolves_to(standalone_proxy, host):
    expected = DNS_CASES[host][1]
    out = _docker("exec", standalone_proxy, "node", "-e", LOCAL_CONNECT, f"{host}:443").stdout.strip()
    assert out.startswith(expected), (host, out)
    if expected.endswith("403"):
        reasons = [json.loads(line).get("reason") for line in
                   _docker("logs", standalone_proxy).stdout.splitlines()
                   if line.startswith("{") and f'"host":"{host}"' in line]
        assert "resolves_to_forbidden_address" in reasons, reasons
