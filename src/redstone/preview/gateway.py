"""Preview gateway (Phase 6): the only thing that serves preview content.

A separate ASGI app on its own listener, never mounted inside Redstone's
API. Every preview is addressed by its own origin, `pv<id>.<domain>`, so
the generated JavaScript it serves can never share cookies, storage or
same-origin fetch with Redstone or with another preview.

Security model:

  * Upstream selection is entirely server-side. `Host` is used only to
    read which preview id the browser is asking for; that id is looked up in
    PreviewManager, and the upstream (a loopback relay port plus its secret
    token) comes from there. No header, path or query can name a host or
    port, so the gateway cannot be turned into an SSRF proxy.
    `X-Forwarded-*`, `Forwarded` and `X-Real-IP` are ignored and stripped.
  * Paths must be plain origin-form: traversal segments, encoded slashes or
    backslashes, NUL, double encoding and control characters are refused
    before anything is forwarded.
  * Bounded: methods, request body, concurrent requests per preview,
    time to first byte, total response time and response bytes.
  * Responses: infrastructure headers are dropped; `Set-Cookie` loses any
    `Domain` attribute (host-only cookies, no tossing onto sibling
    previews); a redirect outside the preview's own origin is refused;
    `nosniff`, `no-referrer`, `frame-ancestors` and COOP are added.
  * WebSockets / upgrades are not supported (refused), so no tunnel exists.
  * Host proxy environment variables are never used (`trust_env=False`).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from urllib.parse import unquote, urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from ..config import PreviewConfig
from ..sandbox.models import RELAY_STATUS_HEADER, RELAY_TOKEN_HEADER
from .manager import PreviewManager
from .models import PREVIEW_ID_PATTERN

__all__ = ["create_preview_gateway_app", "is_safe_target", "rewrite_location",
           "strip_cookie_domain", "METHODS"]

METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_MAX_PATH = 2048
_MAX_QUERY = 4096

_DROP_REQUEST = frozenset({
    "host", "connection", "keep-alive", "proxy-authorization", "proxy-connection", "te",
    "trailer", "transfer-encoding", "upgrade", "content-length", "forwarded", "x-real-ip",
    "via", "expect",
})
_DROP_RESPONSE = frozenset({
    "connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer",
    "upgrade", "server", "x-powered-by", "via", "content-length", RELAY_STATUS_HEADER,
})
# What the app sees as its own origin (the relay rewrites Host to this).
_UPSTREAM_SELF = frozenset({"localhost:5173", "127.0.0.1:5173", "0.0.0.0:5173",
                            "localhost", "127.0.0.1"})


def _has_control(text: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)


def is_safe_target(raw_path: bytes, query: bytes) -> bool:
    """Plain origin-form only. Checked on the raw bytes AND once-decoded, so
    `../`, `%2e%2e`, `%252e%252e`, `%2f`, `%5c`, `\\` and friends are all
    refused rather than normalised into something else."""
    if len(raw_path) > _MAX_PATH or len(query) > _MAX_QUERY:
        return False
    try:
        path = raw_path.decode("ascii")
        query_text = query.decode("ascii")
    except UnicodeDecodeError:
        return False
    if not path.startswith("/") or path.startswith("//"):
        return False
    if "\\" in path or _has_control(path) or _has_control(query_text):
        return False
    lowered = path.lower()
    if any(token in lowered for token in ("%2f", "%5c", "%00", "%25")):
        return False
    decoded = unquote(path)
    if "\\" in decoded or _has_control(decoded):
        return False
    return not any(segment in (".", "..") for segment in decoded.split("/"))


def rewrite_location(value: str) -> str | None:
    """Keep a redirect inside the preview's own origin, or refuse it (None)."""
    value = value.strip()
    if not value or _has_control(value) or "\\" in value or value.startswith("//"):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:                    # e.g. "http://[::1/%zz" -- unparsable means refused
        return None
    if not parts.scheme and not parts.netloc:
        return value                      # relative: resolved against the preview origin
    if parts.scheme in ("http", "https") and parts.netloc.lower() in _UPSTREAM_SELF:
        path = parts.path or "/"
        return path + (f"?{parts.query}" if parts.query else "") + (
            f"#{parts.fragment}" if parts.fragment else "")
    return None                           # another host, or javascript:/data:/…


_COOKIE_DOMAIN = re.compile(r";\s*domain\s*=[^;]*", re.IGNORECASE)


def strip_cookie_domain(value: str) -> str:
    return _COOKIE_DOMAIN.sub("", value)


def create_preview_gateway_app(manager: PreviewManager,
                               config: PreviewConfig | None = None) -> Starlette:
    cfg = config or manager.config
    host_pattern = re.compile(rf"^({PREVIEW_ID_PATTERN})\.{re.escape(cfg.domain)}(?::[0-9]{{1,5}})?$")
    security_headers = [
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "no-referrer"),
        ("content-security-policy", f"frame-ancestors {cfg.frame_ancestors}"),
        ("cross-origin-opener-policy", "same-origin"),
    ]
    # The client only BUILDS requests (so they carry its timeouts). They are
    # sent through the transport directly: the client's send path runs redirect
    # handling on every 3xx -- it parses the upstream's Location to prepare a
    # "next request" even with follow_redirects=False -- so a hostile
    # `Location: javascript:...` could raise inside it. The bare transport has
    # no redirect, cookie or auth machinery and never looks at host proxy
    # settings.
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(cfg.request_timeout_seconds, connect=5.0),
        follow_redirects=False,
        trust_env=False,          # never route through the host's proxy settings
    )
    transport = httpx.AsyncHTTPTransport(
        limits=httpx.Limits(max_connections=256, max_keepalive_connections=32),
    )
    semaphores: dict[str, asyncio.Semaphore] = {}

    def refuse(status: int, message: str) -> Response:
        response = PlainTextResponse(message, status_code=status)
        for name, value in security_headers:
            response.headers[name] = value
        return response

    async def gateway(request: Request) -> Response:
        match = host_pattern.match((request.headers.get("host") or "").strip().lower())
        if not match:
            return refuse(404, "Preview not found.")
        preview_id = match.group(1)
        upstream = manager.resolve(preview_id)
        if upstream is None:
            if manager.status_of(preview_id) is None:
                return refuse(404, "Preview not found.")
            return refuse(503, "Preview is not running.")

        raw_path = request.scope.get("raw_path") or request.url.path.encode("utf-8")
        query = request.scope.get("query_string") or b""
        if not is_safe_target(raw_path, query):
            return refuse(400, "Invalid preview path.")
        if request.headers.get("upgrade"):
            return refuse(501, "WebSockets are not supported by the preview gateway.")

        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > cfg.max_request_bytes):
            return refuse(413, "Request body too large.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > cfg.max_request_bytes:
                return refuse(413, "Request body too large.")

        semaphore = semaphores.setdefault(preview_id, asyncio.Semaphore(cfg.max_concurrent_requests))
        if semaphore.locked():
            return refuse(429, "Too many requests to this preview.")
        await semaphore.acquire()
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                semaphore.release()

        headers = []
        for raw_name, raw_value in request.headers.raw:
            name = raw_name.decode("latin-1").lower()
            if name in _DROP_REQUEST or name.startswith("x-forwarded-") or name.startswith("x-redstone-"):
                continue
            headers.append((name, raw_value.decode("latin-1")))
        headers.append((RELAY_TOKEN_HEADER, upstream.token))

        target = raw_path.decode("ascii") + (f"?{query.decode('ascii')}" if query else "")
        # Built from trusted server-side state only: loopback + relay port.
        url = f"http://{upstream.host}:{upstream.port}{target}"
        try:
            response = await transport.handle_async_request(
                client.build_request(request.method, url, headers=headers, content=bytes(body)),
            )
        except Exception:   # noqa: BLE001 -- nothing the upstream sends may crash the gateway
            release()
            return refuse(502, "Preview is not responding.")

        async def abandon(status: int, message: str) -> Response:
            await response.aclose()
            release()
            return refuse(status, message)

        if response.headers.get(RELAY_STATUS_HEADER):
            return await abandon(502, "Preview is not responding.")
        length = response.headers.get("content-length")
        if length and length.isdigit() and int(length) > cfg.max_response_bytes:
            return await abandon(502, "The preview response is too large.")

        out: list[tuple[str, str]] = []
        for name, value in response.headers.multi_items():
            lower = name.lower()
            if lower in _DROP_RESPONSE or lower.startswith("x-redstone-"):
                continue
            if lower == "location":
                rewritten = rewrite_location(value)
                if rewritten is None:
                    return await abandon(502, "The preview attempted a redirect outside its origin.")
                value = rewritten
            elif lower == "set-cookie":
                value = strip_cookie_domain(value)
            out.append((lower, value))
        out.extend(security_headers)

        deadline = time.monotonic() + cfg.max_response_seconds

        async def relay_body():
            sent = 0
            try:
                async for chunk in response.aiter_raw():
                    sent += len(chunk)
                    if sent > cfg.max_response_bytes or time.monotonic() > deadline:
                        break
                    yield chunk
            except Exception:   # noqa: BLE001 -- a broken upstream stream just ends the body
                pass
            finally:
                await response.aclose()
                release()

        streaming = StreamingResponse(relay_body(), status_code=response.status_code)
        streaming.raw_headers = [(n.encode("latin-1"), v.encode("latin-1")) for n, v in out]
        return streaming

    async def refuse_websocket(websocket: WebSocket) -> None:
        await websocket.close(code=1008)   # policy violation: no upgrades, no tunnels

    @contextlib.asynccontextmanager
    async def lifespan(app):
        yield
        await transport.aclose()
        await client.aclose()

    return Starlette(
        routes=[
            Route("/{path:path}", gateway, methods=METHODS),
            WebSocketRoute("/{path:path}", refuse_websocket),
        ],
        lifespan=lifespan,
    )
