"""Phase 6 preview -- UNIT and MOCKED tests (no Docker).

UNIT: path, redirect and cookie policy functions; ids and endpoints; config.
MOCKED: PreviewManager on the fake sandbox provider, and the real gateway app
talking over real sockets to a local stand-in relay (a Python HTTP server
that checks the token like the real one). Enforcement against real
containers is proven separately in test_preview.py / test_preview_lifecycle.py.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from starlette.testclient import TestClient

from preview_support import LiveServer, REACT, free_port, raw_request
from redstone.config import PreviewConfig
from redstone.domain.models import EventType, PreviewStatus
from redstone.events.bus import EventBus
from redstone.preview.errors import PreviewError, PreviewErrorCode
from redstone.preview.gateway import (
    create_preview_gateway_app,
    is_safe_target,
    rewrite_location,
    strip_cookie_domain,
)
from redstone.preview.manager import PreviewManager, probe_upstream
from redstone.preview.models import PreviewEndpoint, is_preview_id, new_preview_id
from redstone.runtime.manager import RuntimeManager
from redstone.sandbox.models import RELAY_TOKEN_HEADER, PreviewUpstream, SandboxState
from redstone.workspace.manager import WorkspaceManager
from runtime_fakes import FakeSandboxProvider


# ===================================================== UNIT: path policy

@pytest.mark.parametrize("path", [
    "/", "/index.html", "/assets/app.js", "/foo/bar/", "/a-b_c.d~e", "/%20space",
    "/http://127.0.0.1:8000/", "/@vite/client", "/src/App.tsx",
])
def test_safe_paths(path):
    assert is_safe_target(path.encode(), b"")


@pytest.mark.parametrize("path", [
    "/..", "/../x", "/a/../b", "/a/..", "/./x", "/.", "/%2e%2e/x", "/%2E%2e/x", "/%2e/x",
    "/%252e%252e/x", "/a%2fb", "/a%2Fb", "/a%5cb", "/a\\b", "/a%00b", "//evil", "evil",
    "/a\x01b", "/" + "a" * 3000,
])
def test_unsafe_paths(path):
    assert not is_safe_target(path.encode("latin-1"), b"")


def test_unsafe_query():
    assert not is_safe_target(b"/", b"x=\x01")
    assert not is_safe_target(b"/", b"x" * 5000)
    assert is_safe_target(b"/", b"a=1&b=%2e%2e")    # the query is the app's business


def test_non_ascii_raw_path_is_refused():
    assert not is_safe_target("/é".encode("utf-8"), b"")


@pytest.mark.parametrize("value, expected", [
    ("/landing", "/landing"),
    ("landing", "landing"),
    ("?page=2", "?page=2"),
    ("http://localhost:5173/x?y=1#z", "/x?y=1#z"),
    ("http://127.0.0.1:5173", "/"),
    ("http://internal-redstone-service/", None),
    ("https://evil.example/", None),
    ("http://169.254.169.254/", None),
    ("//evil.example/", None),
    ("/\\evil.example", None),
    ("javascript:alert(1)", None),
    ("data:text/html,x", None),
    ("http://localhost:8000/", None),
    ("", None),
    ("/x\r\nSet-Cookie: a=b", None),
    ("http://[::1/%zz", None),                       # unparsable: refused, never a crash
])
def test_redirect_policy(value, expected):
    assert rewrite_location(value) == expected


def test_cookie_domain_is_stripped():
    assert strip_cookie_domain("a=1; Domain=localhost; Path=/") == "a=1; Path=/"
    assert strip_cookie_domain("a=1; path=/; domain=.example.com; HttpOnly") == "a=1; path=/; HttpOnly"
    assert strip_cookie_domain("a=1; Path=/") == "a=1; Path=/"


# ================================================= UNIT: ids and endpoints

def test_preview_ids_are_opaque_unique_dns_labels():
    ids = {new_preview_id() for _ in range(5000)}
    assert len(ids) == 5000
    assert all(is_preview_id(i) and len(i) == 34 for i in ids)
    for bad in ("pv1", "PV" + "a" * 32, "pv" + "g" * 32, "pv" + "a" * 33, "", None, "../x"):
        assert not is_preview_id(bad)


def test_endpoint_is_a_per_preview_origin():
    pid = new_preview_id()
    assert PreviewEndpoint(pid, "http", "localhost", 8100).url == f"http://{pid}.localhost:8100/"
    assert PreviewEndpoint(pid, "https", "preview.example.com", 443).origin == \
        f"https://{pid}.preview.example.com"
    assert PreviewEndpoint(pid, "https", "preview.example.com", 0).origin == \
        f"https://{pid}.preview.example.com"


# ===================================================== UNIT: configuration

@pytest.mark.parametrize("var, raw, field, expected", [
    ("REDSTONE_PREVIEW_DOMAIN", "preview.example.com", "domain", "preview.example.com"),
    ("REDSTONE_PREVIEW_DOMAIN", "127.0.0.1", "domain", "localhost"),
    ("REDSTONE_PREVIEW_DOMAIN", "evil.com/x", "domain", "localhost"),
    ("REDSTONE_PREVIEW_DOMAIN", "*.example.com", "domain", "localhost"),
    ("REDSTONE_PREVIEW_SCHEME", "https", "scheme", "https"),
    ("REDSTONE_PREVIEW_SCHEME", "ftp", "scheme", "http"),
    ("REDSTONE_PREVIEW_PUBLIC_PORT", "0", "public_port", 0),
    ("REDSTONE_PREVIEW_PUBLIC_PORT", "99999", "public_port", 8100),
    ("REDSTONE_PREVIEW_LISTEN_PORT", "80", "listen_port", 8100),
    ("REDSTONE_PREVIEW_MAX_ACTIVE", "0", "max_active", 4),
    ("REDSTONE_PREVIEW_FRAME_ANCESTORS", "'none'", "frame_ancestors", "'none'"),
    ("REDSTONE_PREVIEW_FRAME_ANCESTORS", "https://redstone.example 'self'", "frame_ancestors",
     "https://redstone.example 'self'"),
    ("REDSTONE_PREVIEW_FRAME_ANCESTORS", "*", "frame_ancestors", "'self'"),
    ("REDSTONE_PREVIEW_FRAME_ANCESTORS", "https://a.example; script-src *", "frame_ancestors", "'self'"),
])
def test_preview_config_env_fails_closed(monkeypatch, var, raw, field, expected):
    monkeypatch.setenv(var, raw)
    assert getattr(PreviewConfig.from_env(), field) == expected


# ================================================ MOCKED: stand-in relay

class _StandInRelay:
    """Behaves like preview_relay.js from the gateway's point of view: token
    required, echoes what it received."""

    def __init__(self):
        self.token = secrets.token_hex(32)
        self.received: list[dict] = []
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self):
                if self.headers.get(RELAY_TOKEN_HEADER) != relay.token:
                    self.send_response(403)
                    self.send_header("x-redstone-relay", "denied")
                    self.end_headers()
                    return
                if self.path.startswith("/__location/"):
                    # A hostile upstream redirect whose Location is not a URL
                    # an HTTP library can parse.
                    self.send_response(302)
                    self.send_header("location", {
                        "/__location/js": "javascript:alert(document.cookie)",
                        "/__location/garbage": "http://[::1/%zz",
                        "/__location/internal": "http://internal-redstone-service/",
                    }[self.path])
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                relay.received.append({"path": self.path, "headers": dict(self.headers.items()),
                                       "body": len(body)})
                payload = json.dumps(relay.received[-1]).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_DELETE = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def upstream(self):
        return PreviewUpstream("127.0.0.1", self.port, self.token)

    def close(self):
        self.server.shutdown()


@pytest.fixture
def relay():
    stand_in = _StandInRelay()
    yield stand_in
    stand_in.close()


def _manager(tmp_path, relay=None, *, isolated=True, config=None, probe=None, bus=None):
    provider = FakeSandboxProvider()
    provider.is_isolated = isolated
    if relay is not None:
        provider.preview_upstream_value = relay.upstream
    runtimes = RuntimeManager(provider, max_startup_seconds=2.0, health_poll_interval=0.02)
    previews = PreviewManager(runtimes, config or PreviewConfig(ready_timeout_seconds=1.0),
                              event_bus=bus, probe=probe or probe_upstream, probe_interval=0.02)
    return provider, runtimes, previews


def _workspace(tmp_path, name="ws_unit"):
    return WorkspaceManager(tmp_path / "workspaces").create(name)


def test_start_is_ready_only_after_an_end_to_end_answer(tmp_path, relay):
    _, _, previews = _manager(tmp_path, relay)
    preview = previews.start("prj_u", _workspace(tmp_path), REACT)
    assert preview.status is PreviewStatus.READY
    assert relay.received and relay.received[0]["path"] == "/"


def test_describe_and_events_never_carry_the_capability_or_the_relay(tmp_path, relay):
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    _, _, previews = _manager(tmp_path, relay, bus=bus)
    preview = previews.start("prj_u", _workspace(tmp_path), REACT)

    described = json.dumps(previews.describe(preview))
    assert relay.token not in described and str(relay.port) not in described
    types = [e.type for e in events]
    assert types == [EventType.PREVIEW_CREATED, EventType.PREVIEW_STARTING, EventType.PREVIEW_READY]
    for event in events:
        text = json.dumps(event.payload)
        assert preview.id not in text and relay.token not in text and str(relay.port) not in text


def test_start_is_idempotent_while_ready(tmp_path, relay):
    _, _, previews = _manager(tmp_path, relay)
    workspace = _workspace(tmp_path)
    assert previews.start("prj_u", workspace, REACT).id == previews.start("prj_u", workspace, REACT).id


def test_every_start_after_a_stop_is_a_new_capability(tmp_path, relay):
    _, _, previews = _manager(tmp_path, relay)
    workspace = _workspace(tmp_path)
    first = previews.start("prj_u", workspace, REACT)
    previews.stop("prj_u")
    second = previews.start("prj_u", workspace, REACT)
    assert second.id != first.id
    assert previews.status_of(first.id) is None
    assert previews.resolve(first.id) is None


def test_a_provider_that_does_not_isolate_is_refused_before_anything_runs(tmp_path, relay):
    provider, _, previews = _manager(tmp_path, relay, isolated=False)
    with pytest.raises(PreviewError) as caught:
        previews.start("prj_u", _workspace(tmp_path), REACT)
    assert caught.value.code is PreviewErrorCode.UNAVAILABLE
    assert provider.create_calls == []


def test_no_upstream_fails_and_tears_down_the_runtime(tmp_path):
    provider, _, previews = _manager(tmp_path, relay=None)
    with pytest.raises(PreviewError) as caught:
        previews.start("prj_u", _workspace(tmp_path), REACT)
    assert caught.value.code is PreviewErrorCode.UNAVAILABLE
    assert provider.live_sandbox_ids() == set()
    assert previews.get_for_project("prj_u").status is PreviewStatus.FAILED


def test_never_answering_app_fails_and_tears_down(tmp_path, relay):
    provider, _, previews = _manager(tmp_path, relay, probe=lambda upstream: False)
    with pytest.raises(PreviewError) as caught:
        previews.start("prj_u", _workspace(tmp_path), REACT)
    assert caught.value.code is PreviewErrorCode.START_FAILED
    assert provider.live_sandbox_ids() == set()


def test_busy_and_limit(tmp_path, relay):
    _, _, previews = _manager(tmp_path, relay, config=PreviewConfig(max_active=1, ready_timeout_seconds=1))
    lock = previews._project_lock("prj_busy")
    lock.acquire()
    try:
        with pytest.raises(PreviewError) as busy:
            previews.start("prj_busy", _workspace(tmp_path, "ws_busy"), REACT)
        assert busy.value.code is PreviewErrorCode.BUSY
    finally:
        lock.release()
    previews.start("prj_one", _workspace(tmp_path, "ws_one"), REACT)
    with pytest.raises(PreviewError) as limit:
        previews.start("prj_two", _workspace(tmp_path, "ws_two"), REACT)
    assert limit.value.code is PreviewErrorCode.LIMIT_REACHED


def test_stop_destroy_idempotency_and_unknowns(tmp_path, relay):
    _, _, previews = _manager(tmp_path, relay)
    with pytest.raises(PreviewError):
        previews.get_for_project("nope")
    assert previews.destroy("nope") is False
    previews.start("prj_u", _workspace(tmp_path), REACT)
    assert previews.stop("prj_u").status is PreviewStatus.STOPPED
    assert previews.stop("prj_u").status is PreviewStatus.STOPPED
    assert previews.destroy("prj_u") is True
    assert previews.destroy("prj_u") is False


def test_sweep_enforces_idle_lifetime_and_notices_crashes(tmp_path, relay):
    provider, runtimes, previews = _manager(
        tmp_path, relay, config=PreviewConfig(idle_timeout_seconds=60, max_lifetime_seconds=600,
                                              ready_timeout_seconds=1))
    workspace = _workspace(tmp_path)
    preview = previews.start("prj_u", workspace, REACT)
    assert previews.sweep(now=preview.last_activity + timedelta(seconds=30)) == \
        {"expired": [], "idle": [], "crashed": []}
    assert previews.sweep(now=preview.last_activity + timedelta(seconds=61))["idle"] == ["prj_u"]

    preview = previews.start("prj_u", workspace, REACT)
    assert previews.sweep(now=preview.created_at + timedelta(seconds=601))["expired"] == ["prj_u"]

    preview = previews.start("prj_u", workspace, REACT)
    provider.force_state(runtimes.get(preview.runtime_id, "prj_u").sandbox_id, SandboxState.FAILED)
    assert previews.sweep()["crashed"] == ["prj_u"]
    assert previews.get_for_project("prj_u").status is PreviewStatus.FAILED


# ============================================ MOCKED: gateway on real sockets

@pytest.fixture
def served(tmp_path, relay, monkeypatch):
    # A hostile host proxy setting must not be honoured by the gateway.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    port = free_port()
    config = PreviewConfig(public_port=port, listen_port=port, ready_timeout_seconds=1,
                           max_request_bytes=1000)
    _, _, previews = _manager(tmp_path, relay, config=config)
    preview = previews.start("prj_g", _workspace(tmp_path), REACT)
    with LiveServer(create_preview_gateway_app(previews, config), port=port):
        yield port, preview, previews, relay


def test_gateway_forwards_with_the_token_and_strips_forwarding_headers(served):
    port, preview, _, relay = served
    status, _, body = raw_request(port, f"{preview.id}.localhost:{port}", "/x/y?z=1", headers={
        "X-Forwarded-Host": "evil", "X-Forwarded-For": "10.0.0.1", "Forwarded": "for=1.2.3.4",
        "X-Real-IP": "127.0.0.1", RELAY_TOKEN_HEADER: "attacker", "X-Redstone-Anything": "1",
        "Cookie": "app=1",
    })
    assert status == 200, body
    received = relay.received[-1]
    lowered = {k.lower(): v for k, v in received["headers"].items()}
    assert received["path"] == "/x/y?z=1"
    assert lowered[RELAY_TOKEN_HEADER] == relay.token          # the gateway's, never the client's
    for name in lowered:
        assert not name.startswith("x-forwarded-") and name not in ("forwarded", "x-real-ip",
                                                                   "x-redstone-anything")
    assert lowered["cookie"] == "app=1"                          # the app's own cookies pass


def test_gateway_status_codes(served):
    port, preview, previews, _ = served
    host = f"{preview.id}.localhost:{port}"
    assert raw_request(port, "internal-service", "/")[0] == 404
    assert raw_request(port, f"pv{'0' * 32}.localhost:{port}", "/")[0] == 404
    assert raw_request(port, host, "/../x")[0] == 400
    assert raw_request(port, host, "/", method="POST", body=b"x" * 1001)[0] == 413
    assert raw_request(port, host, "/", method="TRACE")[0] == 405
    previews.stop("prj_g")
    assert raw_request(port, host, "/")[0] == 503


@pytest.mark.parametrize("route", ["/__location/js", "/__location/garbage", "/__location/internal"])
def test_hostile_redirects_are_refused_cleanly_never_a_server_error(served, route):
    """Regression: an unparsable Location once raised inside the HTTP
    client's redirect handling and surfaced as a 500."""
    port, preview, _, _ = served
    host = f"{preview.id}.localhost:{port}"
    status, headers, _ = raw_request(port, host, route)
    assert status == 502, status
    assert not [v for k, v in headers if k.lower() == "location"]
    # ...and the concurrency slot was released: the preview still answers.
    assert raw_request(port, host, "/")[0] == 200


def test_gateway_refuses_websockets(served):
    port, preview, previews, _ = served
    client = TestClient(create_preview_gateway_app(previews, previews.config))
    with pytest.raises(Exception):
        with client.websocket_connect("/", headers={"host": f"{preview.id}.localhost:{port}"}):
            pass
