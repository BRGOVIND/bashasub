"""Phase 6 live preview -- REAL DOCKER (skipped, with an explicit reason, when
no daemon is reachable).

Two hostile previews, A and B, in two workspaces, served through a real
preview gateway listening on a real socket. The app inside each preview is
tests/redstone/fixtures/preview_app: it probes for everything at startup and
its routes try to subvert the gateway. Every assertion below is an outcome
observed from real containers, real networks and real HTTP.

Threat model: the generated app is malicious; the browser is untrusted.
"""

from __future__ import annotations

import json
import threading
import time
import uuid

import pytest

from preview_support import (
    LiveServer,
    REACT,
    docker,
    free_port,
    get_json,
    header,
    inspect,
    make_workspace,
    network,
    preview_host,
    raw_request,
)
from redstone.config import PreviewConfig
from redstone.domain.models import PreviewStatus
from redstone.preview.gateway import create_preview_gateway_app
from redstone.preview.manager import PreviewManager
from redstone.runtime.manager import RuntimeManager
from redstone.sandbox.models import RELAY_TOKEN_HEADER
from redstone.sandbox.providers.docker_provider import DockerSandboxProvider, docker_available

pytestmark = pytest.mark.skipif(not docker_available(), reason="Docker daemon not reachable")

MAX_RESPONSE = 2 * 1024 * 1024


class Stack:
    pass


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    root = tmp_path_factory.mktemp("preview")
    port = free_port()
    config = PreviewConfig(public_port=port, listen_port=port, max_active=4,
                           request_timeout_seconds=4.0, max_response_bytes=MAX_RESPONSE,
                           max_concurrent_requests=3)
    runtimes = RuntimeManager(DockerSandboxProvider(), max_startup_seconds=60)
    previews = PreviewManager(runtimes, config)

    s = Stack()
    s.port, s.config, s.runtimes, s.previews = port, config, runtimes, previews
    s.ws_b = make_workspace(root, "ws_preview_b", identity="PREVIEW-B")
    s.b = previews.start("prj_b", s.ws_b, REACT)
    s.b_sandbox = runtimes.get(s.b.runtime_id, "prj_b").sandbox_id
    s.b_upstream = runtimes.provider.preview_upstream(s.b_sandbox)

    # A probes B's app, B's relay, and B's relay via the host, by name/port.
    targets = [["other_app", s.b_sandbox, 5173], ["other_relay", f"{s.b_sandbox}-relay", 8080],
               ["other_relay_via_host", "host.docker.internal", s.b_upstream.port],
               ["gateway_via_host", "host.docker.internal", port]]
    s.ws_a = make_workspace(root, "ws_preview_a", identity="PREVIEW-A",
                            extra={"probe-targets.json": json.dumps(targets)})
    s.a = previews.start("prj_a", s.ws_a, REACT)
    s.a_sandbox = runtimes.get(s.a.runtime_id, "prj_a").sandbox_id
    s.a_upstream = runtimes.provider.preview_upstream(s.a_sandbox)
    s.host_a = preview_host(s.a.id, port)
    s.host_b = preview_host(s.b.id, port)

    with LiveServer(create_preview_gateway_app(previews, config), port=port):
        yield s
    previews.destroy("prj_a")
    previews.destroy("prj_b")


def _get(stack, host, path, **kwargs):
    return raw_request(stack.port, host, path, **kwargs)


# =========================================== A-C: lifecycle and reachability

def test_previews_are_ready_on_their_own_origins(stack):
    for preview in (stack.a, stack.b):
        assert preview.status is PreviewStatus.READY
        described = stack.previews.describe(preview)
        assert described["url"] == f"http://{preview.id}.localhost:{stack.port}/"
        assert set(described) == {"preview_id", "project_id", "status", "url", "last_error",
                                  "created_at", "last_activity"}
    assert stack.a.id != stack.b.id
    assert len(stack.a.id) == 34 and stack.a.id.startswith("pv")


def test_gateway_serves_each_preview_its_own_content(stack):
    assert _get(stack, stack.host_a, "/__whoami")[2] == b"PREVIEW-A"
    assert _get(stack, stack.host_b, "/__whoami")[2] == b"PREVIEW-B"


# ============================================= topology (docker inspect)

def test_topology_and_hardening(stack):
    sid = stack.a_sandbox
    app, relay = inspect(sid), inspect(f"{sid}-relay")

    for container in (app, relay):
        host = container["HostConfig"]
        assert host["Privileged"] is False
        assert "ALL" in (host["CapDrop"] or [])
        assert not host.get("CapAdd")
        assert "no-new-privileges" in (host["SecurityOpt"] or [])
        assert host["ReadonlyRootfs"] is True
        assert not host.get("Devices")
        assert host["NetworkMode"] not in ("host", "bridge", "default")
        assert container["Config"]["User"] == "1000:1000"
        assert all("docker.sock" not in (m.get("Source") or "") for m in container["Mounts"])

    # The app: one --internal network, nothing published.
    assert set(app["NetworkSettings"]["Networks"]) == {f"{sid}-net"}
    assert not app["HostConfig"].get("PortBindings")
    assert not any(app["NetworkSettings"].get("Ports", {}).values())
    # The relay: internal + its own bridge, exactly one port, loopback only.
    assert set(relay["NetworkSettings"]["Networks"]) == {f"{sid}-net", f"{sid}-pub"}
    bindings = relay["HostConfig"]["PortBindings"]
    assert list(bindings) == ["8080/tcp"]
    assert [b["HostIp"] for b in bindings["8080/tcp"]] == ["127.0.0.1"]

    internal, publish = network(f"{sid}-net"), network(f"{sid}-pub")
    assert internal["Internal"] is True
    assert {c["Name"] for c in internal["Containers"].values()} == {sid, f"{sid}-relay"}
    assert {c["Name"] for c in publish["Containers"].values()} == {f"{sid}-relay"}
    assert publish["Options"].get("com.docker.network.bridge.enable_icc") == "false"

    labels = relay["Config"]["Labels"]
    assert labels["redstone.managed"] == "true"
    assert labels["redstone.role"] == "preview-relay"
    assert labels["redstone.project_id"] == "prj_a"
    assert labels["redstone.workspace_id"] == "ws_preview_a"
    assert labels["redstone.runtime_id"] == stack.a.runtime_id
    assert app["Config"]["Labels"]["redstone.purpose"] == "start_preview_server"


# ================================= D-E: the relay is not reachable without a token

def test_relay_refuses_requests_without_the_token(stack):
    port = stack.a_upstream.port
    for headers in ({}, {RELAY_TOKEN_HEADER: "0" * 64}, {RELAY_TOKEN_HEADER: stack.b_upstream.token}):
        status, _, _ = raw_request(port, "anything", "/__whoami", headers=headers)
        assert status == 403, headers
    status, _, body = raw_request(port, "anything", "/__whoami",
                                  headers={RELAY_TOKEN_HEADER: stack.a_upstream.token})
    assert (status, body) == (200, b"PREVIEW-A")


def test_unrelated_container_cannot_use_the_relay_or_reach_the_app(stack):
    name = f"rs-preview-outsider-{uuid.uuid4().hex[:8]}"
    script = (f"wget -S -T4 -qO- http://host.docker.internal:{stack.a_upstream.port}/__whoami 2>&1; "
              f"echo; wget -T3 -qO- http://{stack.a_sandbox}:5173/ 2>&1; echo END")
    out = docker("run", "--rm", "--name", name, "alpine:3.19", "sh", "-c", script).stdout
    assert "403" in out
    assert "PREVIEW-A" not in out


# ====================================== F-J: the preview app has no network

def test_preview_app_is_contained(stack):
    deadline = time.monotonic() + 90
    probes = get_json(stack.port, stack.host_a, "/__probe")
    while not probes.get("done") and time.monotonic() < deadline:
        time.sleep(1.0)
        probes = get_json(stack.port, stack.host_a, "/__probe")
    assert probes["done"] is True, "the app must finish probing every destination"
    assert probes["uid"] == 1000
    assert probes["secret_env"] == []
    host_derived = {"USERPROFILE", "COMPUTERNAME", "USERNAME", "APPDATA", "LOCALAPPDATA",
                    "GEMINI_API_KEY", "AI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                    "DOCKER_HOST", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"}
    assert not host_derived & set(probes["env_keys"]), probes["env_keys"]
    for name, outcome in probes["read"].items():
        assert outcome["ok"] is False, name
    assert probes["docker_sock_exists"] is False
    for name, outcome in probes["write"].items():
        assert outcome["ok"] is False, name
    assert probes["symlink_outside"]["ok"] is False
    for name, outcome in probes["net"].items():
        assert outcome["ok"] is False, (name, outcome)
    assert not probes["dns"].startswith("RESOLVED"), probes["dns"]


# ==================================================== K-L: cross-preview

def test_host_selects_only_its_own_preview(stack):
    # A's origin with B's id anywhere in the path still reaches A only.
    for path in (f"/{stack.b.id}/__whoami", f"/__whoami?preview={stack.b.id}",
                 f"/preview/{stack.b.id}/__whoami"):
        status, _, body = _get(stack, stack.host_a, path)
        assert b"PREVIEW-B" not in body, path


@pytest.mark.parametrize("host", [
    "localhost", "127.0.0.1", "", "internal-redstone-service", "pv1.localhost",
    "pv" + "0" * 32 + ".localhost",                 # well-formed, never issued
    "pv" + "a" * 31 + ".localhost",                 # one character short
    "pv" + "g" * 32 + ".localhost",                 # not hex
    "evil.pv" + "0" * 32 + ".localhost",
])
def test_malformed_or_unknown_preview_hosts_are_not_found(stack, host):
    status, _, body = _get(stack, host or "x", "/__whoami")   # empty Host -> a bare label
    assert status == 404
    assert b"PREVIEW" not in body


def test_preview_id_on_another_domain_is_not_found(stack):
    for host in (f"{stack.a.id}.example.com", f"{stack.a.id}.localhost.evil.example",
                 f"{stack.a.id}.localhost@evil.example"):
        assert _get(stack, host, "/__whoami")[0] == 404


# ================================================ M-N: path traversal

@pytest.mark.parametrize("path", [
    "/../__whoami", "/a/../__whoami", "/%2e%2e/__whoami", "/%2E%2E/__whoami",
    "/%252e%252e/__whoami", "/a%2f..%2f__whoami", "/a%5c..%5c__whoami", "/a\\..\\__whoami",
    "/./__whoami", "/%2e/__whoami", "//__whoami", "/a%00b", f"/../../preview/pv{'0' * 32}/",
])
def test_traversal_is_refused_before_anything_is_forwarded(stack, path):
    before = int(_get(stack, stack.host_b, "/__count")[2])
    status, _, _ = _get(stack, stack.host_b, path)
    after = int(_get(stack, stack.host_b, "/__count")[2])
    assert status == 400, path
    assert after == before + 1, "only the /__count probe itself may reach the app"


# ======================================== O-P: Host / forwarding headers

def test_forwarding_headers_cannot_steer_or_reach_the_app(stack):
    echoed = get_json(stack.port, stack.host_a, "/__echo")
    status, _, body = _get(stack, stack.host_a, "/__echo", headers={
        "X-Forwarded-Host": stack.host_b, "X-Forwarded-For": "10.0.0.1", "X-Real-IP": "127.0.0.1",
        "Forwarded": f"host={stack.host_b}", "X-Forwarded-Proto": "https",
        RELAY_TOKEN_HEADER: stack.b_upstream.token,
    })
    seen = json.loads(body)["headers"]
    assert status == 200
    assert seen["host"] == "localhost:5173" == echoed["headers"]["host"]
    for name in seen:
        assert not name.startswith("x-forwarded-") and name not in ("forwarded", "x-real-ip",
                                                                   RELAY_TOKEN_HEADER), name
    assert _get(stack, stack.host_a, "/__whoami", headers={"X-Forwarded-Host": stack.host_b})[2] == b"PREVIEW-A"


# ============================================ Q-T: no upstream from the URL

@pytest.mark.parametrize("path", [
    "/http://127.0.0.1:8000/", "/http://169.254.169.254/latest/meta-data/",
    "/http://10.0.0.1/", "/http://172.16.0.1/", "/http://192.168.0.1/", "/http://[::1]/",
    f"/http://{'x' * 5}.localhost/",
])
def test_urls_in_the_path_are_just_paths_to_the_same_app(stack, path):
    """The upstream comes from server-side state only: a URL in the path is
    delivered to A's own app as a literal path (its catch-all page answers),
    and no connection is made anywhere else."""
    before = int(_get(stack, stack.host_a, "/__count")[2])
    status, _, body = _get(stack, stack.host_a, path)
    after = int(_get(stack, stack.host_a, "/__count")[2])
    assert status == 200 and b"PREVIEW-A" in body, path
    assert after == before + 2, "exactly this request and the counter reached A's app"


# ========================================== response header policy

def test_infrastructure_headers_are_stripped_and_security_headers_added(stack):
    status, headers, _ = _get(stack, stack.host_a, "/__headers")
    assert status == 200
    # Neither the app's spoofed Server nor the gateway's own (uvicorn) leaks.
    assert header(headers, "server") == []
    assert header(headers, "x-powered-by") == []
    assert header(headers, "via") == []
    assert header(headers, "x-redstone-relay") == []
    assert header(headers, "x-custom") == ["kept"]
    assert header(headers, "x-content-type-options") == ["nosniff"]
    assert header(headers, "referrer-policy") == ["no-referrer"]
    assert "frame-ancestors 'self'" in header(headers, "content-security-policy")


def test_cookies_lose_their_domain_attribute(stack):
    _, headers, _ = _get(stack, stack.host_a, "/__cookie")
    cookies = header(headers, "set-cookie")
    assert len(cookies) == 3
    assert all("domain" not in c.lower() for c in cookies), cookies
    assert any(c.startswith("plain=2") for c in cookies)


@pytest.mark.parametrize("route, expected", [
    ("/__redirect/internal", None),
    ("/__redirect/metadata", None),
    ("/__redirect/js", None),
    ("/__redirect/protocol-relative", None),
    ("/__redirect/relative", "/landing"),
    ("/__redirect/self", "/landing?x=1"),
])
def test_redirects_cannot_leave_the_preview_origin(stack, route, expected):
    status, headers, _ = _get(stack, stack.host_a, route)
    if expected is None:
        assert status == 502, route
        assert header(headers, "location") == []
    else:
        assert status == 302
        assert header(headers, "location") == [expected]


# ================================================================ limits

def test_oversized_response_is_truncated(stack):
    status, _, body = _get(stack, stack.host_a, "/__big")
    assert status == 200
    assert len(body) <= MAX_RESPONSE


def test_oversized_request_body_is_refused(stack):
    status, _, _ = _get(stack, stack.host_a, "/__body", method="POST", body=b"x" * (1024 * 1024 + 1))
    assert status == 413
    status, _, body = _get(stack, stack.host_a, "/__body", method="POST", body=b"x" * 1000)
    assert (status, body) == (200, b"1000")


def test_hanging_upstream_times_out(stack):
    status, _, _ = _get(stack, stack.host_a, "/__slow", timeout=30)
    assert status == 502


def test_concurrent_requests_per_preview_are_bounded(stack):
    results = []

    def slow():
        results.append(_get(stack, stack.host_b, "/__slow", timeout=30)[0])

    threads = [threading.Thread(target=slow) for _ in range(stack.config.max_concurrent_requests)]
    for thread in threads:
        thread.start()
    time.sleep(1.0)
    assert _get(stack, stack.host_b, "/__whoami")[0] == 429
    for thread in threads:
        thread.join()
    assert _get(stack, stack.host_b, "/__whoami")[2] == b"PREVIEW-B"


def test_methods_outside_the_allowlist_are_refused(stack):
    for method in ("TRACE", "CONNECT", "PROPFIND"):
        path = f"{stack.a.id}.localhost:443" if method == "CONNECT" else "/"
        status, _, _ = _get(stack, stack.host_a, path, method=method)
        assert status in (400, 404, 405), method


def test_fork_bomb_inside_the_preview_is_bounded(stack):
    outcome = get_json(stack.port, stack.host_a, "/__fork")
    assert outcome["alive"] < 128, outcome     # --pids-limit


# ============================================================ websockets

def test_websocket_upgrades_are_refused_by_gateway_and_relay(stack):
    ws_headers = {"Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
                  "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="}
    status, _, _ = _get(stack, stack.host_a, "/", headers=ws_headers)
    assert status in (403, 404, 426, 501), status
    status, _, _ = raw_request(stack.a_upstream.port, "x", "/", headers={
        **ws_headers, RELAY_TOKEN_HEADER: stack.a_upstream.token})
    assert status == 501
