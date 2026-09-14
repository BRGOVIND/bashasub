"""Phase 6 browser-origin isolation -- REAL BROWSER + REAL DOCKER.

Real Chrome (via Playwright) loads a hostile preview's page from the real
preview gateway, next to a stand-in for an authenticated Redstone origin
(Redstone has no user authentication yet, so the stand-in plays that role:
it sets a session cookie -- HttpOnly and not -- and a localStorage token, and
answers /api/secret only to that cookie). The preview's JavaScript then tries
to read and use all of it, to read another preview, to reach into the page
embedding it, and to toss cookies onto its siblings.

Skipped with an explicit reason when Playwright or Docker is unavailable.
"""

from __future__ import annotations

import html
import json
import uuid
from urllib.parse import quote

import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from preview_support import LiveServer, REACT, free_port, make_workspace
from redstone.config import PreviewConfig
from redstone.preview.gateway import create_preview_gateway_app
from redstone.preview.manager import PreviewManager
from redstone.runtime.manager import RuntimeManager
from redstone.sandbox.providers.docker_provider import DockerSandboxProvider, docker_available

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="Playwright is not installed; browser isolation cannot be exercised")
pytestmark = pytest.mark.skipif(not docker_available(), reason="Docker daemon not reachable")

SECRET = f"REDSTONE-SESSION-{uuid.uuid4().hex}"


def _stand_in_redstone():
    seen: list[dict] = []

    async def home(request):
        page = HTMLResponse(
            f"<title>redstone</title><script>localStorage.setItem('redstone_token','{SECRET}')</script>"
            "<p>redstone</p>")
        page.set_cookie("redstone_session", SECRET, httponly=True, samesite="lax")
        page.set_cookie("redstone_visible", SECRET, httponly=False, samesite="lax")
        return page

    async def secret(request):
        carried = request.cookies.get("redstone_session") == SECRET
        seen.append({"cookie": carried, "site": request.headers.get("sec-fetch-site")})
        return PlainTextResponse("TOP-SECRET" if carried else "anonymous")

    async def embed(request):
        src = html.escape(request.query_params["src"], quote=True)
        return HTMLResponse(f"""<title>redstone embedder</title>
<iframe id="f" src="{src}" width="600" height="400"></iframe>
<pre id="parent-read">pending</pre><pre id="messages"></pre>
<script>
window.addEventListener("message", (e) => {{
  document.getElementById("messages").textContent += e.origin + "\\n";
}});
document.getElementById("f").addEventListener("load", () => {{
  let result;
  try {{ result = "READ:" + document.getElementById("f").contentDocument.title; }}
  catch (e) {{ result = "BLOCKED:" + e.name; }}
  document.getElementById("parent-read").textContent = result;
}});
</script>""")

    async def seen_view(request):
        return JSONResponse(seen)

    app = Starlette(routes=[Route("/", home), Route("/api/secret", secret),
                            Route("/embed", embed), Route("/seen", seen_view)])
    return app


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("browser")
    harness = LiveServer(_stand_in_redstone())
    port = free_port()
    config = PreviewConfig(public_port=port, listen_port=port, ready_timeout_seconds=30,
                           frame_ancestors=f"http://127.0.0.1:{harness.port}")
    runtimes = RuntimeManager(DockerSandboxProvider(), max_startup_seconds=60)
    previews = PreviewManager(runtimes, config)
    a = previews.start("prj_browser_a", make_workspace(root, "ws_browser_a", identity="A"), REACT)
    b = previews.start("prj_browser_b", make_workspace(root, "ws_browser_b", identity="B"), REACT)
    try:
        with harness, LiveServer(create_preview_gateway_app(previews, config), port=port):
            yield {"harness": f"http://127.0.0.1:{harness.port}",
                   "a": f"http://{a.id}.localhost:{port}", "b": f"http://{b.id}.localhost:{port}"}
    finally:
        previews.destroy("prj_browser_a")
        previews.destroy("prj_browser_b")


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            instance = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:   # noqa: BLE001
            pytest.skip(f"no Chrome for Playwright to drive: {type(exc).__name__}")
        yield instance
        instance.close()


def _attack(page, origin, world):
    url = f"{origin}/attack.html?api={quote(world['harness'])}&other={quote(world['b'] if origin == world['a'] else world['a'])}"
    page.goto(url)
    page.wait_for_function("document.getElementById('out').textContent !== 'pending'", timeout=30000)
    return json.loads(page.text_content("#out"))


def test_preview_javascript_is_isolated_from_redstone_and_other_previews(browser, world):
    context = browser.new_context()
    page = context.new_page()
    page.goto(world["harness"] + "/")                    # the "logged-in" Redstone origin
    assert any(c["name"] == "redstone_session" for c in context.cookies(world["harness"]))

    page.goto(world["a"] + "/__cookie")                  # A tries to toss via Set-Cookie Domain=
    a = _attack(page, world["a"], world)
    b = _attack(page, world["b"], world)

    # Redstone's cookies (HttpOnly or not) and storage are invisible to A.
    for field in ("cookie_before", "cookie"):
        assert SECRET not in a[field] and "redstone_" not in a[field], a[field]
    assert "redstone_token" not in a["localStorage"]
    # A cannot read Redstone's API, even with ambient credentials...
    assert a["api_fetch"].startswith("BLOCKED"), a["api_fetch"]
    # ...a no-cors request only yields an opaque response...
    assert a["api_no_cors"] in ("RESPONSE_TYPE:opaque",) or a["api_no_cors"].startswith("BLOCKED")
    # ...and neither request carried the session cookie (SameSite=Lax, cross-site).
    import urllib.request
    seen = json.loads(urllib.request.urlopen(world["harness"] + "/seen", timeout=10).read())
    assert seen and not any(entry["cookie"] for entry in seen), seen
    # A cannot read another preview, or a frame of Redstone's origin.
    assert a["other_fetch"].startswith("BLOCKED"), a["other_fetch"]
    assert a["iframe_read"].startswith("BLOCKED"), a["iframe_read"]

    # B sees nothing A left behind: no storage, no own-cookie, no tossed cookie.
    assert b["marker_before"] is None, b["marker_before"]
    assert "own=" + world["a"].split("//")[1].split(":")[0] not in b["cookie_before"]
    assert "tossed_by_header" not in b["cookie_before"], b["cookie_before"]
    assert "tossed_by_js" not in b["cookie_before"], b["cookie_before"]
    context.close()


def test_an_embedded_preview_cannot_reach_into_its_embedder(browser, world):
    context = browser.new_context()
    page = context.new_page()
    page.goto(world["harness"] + "/")
    attack = f"{world['a']}/attack.html?api={quote(world['harness'])}&other={quote(world['b'])}"
    page.goto(f"{world['harness']}/embed?src={quote(attack)}")

    frame = next(f for f in page.frames if f.url.startswith(world["a"]))
    frame.wait_for_function("document.getElementById('out').textContent !== 'pending'", timeout=30000)
    inside = json.loads(frame.text_content("#out"))
    assert inside["parent_doc"].startswith("BLOCKED"), inside["parent_doc"]
    assert inside["parent_storage"].startswith("BLOCKED"), inside["parent_storage"]

    page.wait_for_function("document.getElementById('parent-read').textContent !== 'pending'")
    assert page.text_content("#parent-read").startswith("BLOCKED")
    # A postMessage arrives, and it is stamped with the PREVIEW's origin -- the
    # embedder can and must check event.origin before trusting it.
    page.wait_for_function("document.getElementById('messages').textContent.length > 0")
    assert page.text_content("#messages").strip() == world["a"]
    context.close()


def test_default_frame_ancestors_block_foreign_embedding(browser, world):
    """The gateway's CSP frame-ancestors is the embedding allowlist; an
    origin that is not on it (here: preview B trying to frame preview A)
    gets nothing."""
    context = browser.new_context()
    page = context.new_page()
    page.goto(world["b"] + "/")
    page.set_content(f'<iframe id="f" src="{world["a"]}/"></iframe>')
    page.wait_for_timeout(2000)
    framed = [f for f in page.frames if f.url.startswith(world["a"])]
    content = framed[0].content() if framed else ""
    assert "preview A" not in content
    context.close()
