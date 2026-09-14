# Redstone live preview

Phase 6. The backend path from a generated project to a browser:

```
coding agent → project files → install (egress proxy) → preview runtime (sandbox)
            → preview relay → preview gateway → browser, on the preview's own origin
```

There is no polished frontend yet. This document covers the runtime, network,
gateway and browser-origin boundaries that one will sit on.

## Threat model

**Two untrusted parties:**
- **The generated application** — its React/TS/JS, HTML, CSS, Vite config,
  `package.json` scripts, lifecycle scripts and dev server — is **malicious**.
- **The browser** is **untrusted** too: it sends any `Host`, any headers and
  any path.

**Four independent boundaries**, each tested on its own:
1. **Docker runtime**: the app runs only inside a sandbox.
2. **Workspace**: one project's files.
3. **Preview relay + gateway**: the only way HTTP reaches the app.
4. **Browser origin**: per-preview origins, separate from Redstone.

"The preview loads in my browser" is not treated as evidence of any of them.

**Labels:**

| Label | Meaning |
|---|---|
| **DOCKER-ENFORCED** | a Docker/kernel mechanism blocks it; a real-Docker test observed the block |
| **GATEWAY-ENFORCED** | Redstone's gateway or relay refuses it; a real-socket test observed the refusal |
| **BROWSER-ENFORCED** | the browser's same-origin policy blocks it; a real-Chrome test observed the block |
| **ENFORCED** | enforced by Redstone's own code by construction (no alternative code path exists) |
| **TESTED** | behaviour verified, not a security boundary in itself |
| **PERIODICALLY ENFORCED** | a sweeper enforces it on a schedule |
| **NOT ENFORCED** | a known gap, stated here |
| **ENVIRONMENT LIMITATION** | can't be reproduced on this machine |
| **RESERVED** | declared for later; nothing uses it |

## Topology

```
                      internet   ✗ (no route)
                         │
  preview app  ── <id>-net (Docker --internal) ── <id>-relay ── <id>-pub ── 127.0.0.1:<port>
  (sandbox)      members: exactly app + relay       │           ICC off,      host loopback,
  no published                                      │           relay only    token required
  port, listens                                     │                              │
  0.0.0.0:5173                                      └──── fixed upstream ─────     │
  in its own netns                                        <app>:5173               │
                                                                                   ▼
                                    Redstone preview gateway (separate ASGI app / origin)
                                                                                   │
                                                          browser at http://pv<id>.<domain>/
```

- **The app** is an ordinary `RuntimeManager` runtime started with
  `preview=True`: operation `START_PREVIEW_SERVER`, `NetworkPolicy.PREVIEW`.
  It carries every Phase 4 control: non-root, `--cap-drop ALL`,
  no-new-privileges, read-only root, bounded `/tmp`, CPU/memory/PID/output
  limits, the periodic storage quota, an explicit environment, and no
  credentials. It binds `0.0.0.0` inside its own network namespace, whose only
  other member is its relay. **DOCKER-ENFORCED**: no route out; nothing
  published; no external DNS.
- **The relay** (`src/redstone/sandbox/preview_relay.js`, same digest-pinned
  image, one per preview) is the app's only reachable peer.
  - It publishes one port, bound to host **loopback**, with a random host port.
  - **Finding (verified on this daemon):** other containers *can* reach that
    loopback-published port via `host.docker.internal`. So the relay never
    trusts position: every request must carry this preview's random 256-bit
    token, compared in constant time.
  - Its upstream is fixed at start (`<sandbox>:5173`), and it rewrites `Host`
    to `localhost:5173`.
  - It strips forwarding headers and the token, refuses `CONNECT` and
    upgrades, and is bounded in concurrency, time and bytes.
  - It is hardened like the egress proxy: non-root, no capabilities, read-only,
    128 MB / 64 PIDs / 0.5 CPU.
- **The gateway** (`redstone.preview.gateway`) is a separate ASGI app on its
  own listener. It is never mounted inside the Redstone API.

## Browser-origin model

Generated JavaScript is *meant* to run. So the question is which origin it
runs in.

- **Each preview gets its own origin**: `{scheme}://pv<128-bit hex>.{domain}[:{port}]/`.
  - A shared preview origin would let preview A `fetch()` preview B
    same-origin and read B's `localStorage`.
  - Previews under Redstone's own origin would expose Redstone's cookies,
    storage and API.
- **Different ports on one host don't isolate cookies**, so previews must be on
  a *different host* from Redstone. The local default is `pv<id>.localhost` —
  a different site from Redstone on `127.0.0.1`. Browsers resolve `*.localhost`
  to loopback.
- **Deployment requirement (not enforceable from code): NOT ENFORCED until
  deployed correctly.** In production the preview domain must be a **separate
  registrable domain** from Redstone's — not a subdomain of it — and ideally
  on the **Public Suffix List**, like `github.io` or `vercel.app`, so that no
  preview can set cookies for its siblings.

Real-Chrome evidence (`tests/redstone/test_preview_browser.py`, Playwright
driving system Chrome, against real Docker previews). The "Redstone origin" is
a stand-in: Redstone has no user authentication yet, so the stand-in sets a
session cookie (both HttpOnly and not), a `localStorage` token, and a
cookie-gated `/api/secret`.

| Attack from preview JavaScript | Result | |
|---|---|---|
| read Redstone's cookies (HttpOnly or not) via `document.cookie` | not visible | **BROWSER-ENFORCED** |
| read Redstone's `localStorage` | not visible | **BROWSER-ENFORCED** |
| `fetch(redstone + "/api/secret", {credentials: "include"})` | blocked (no CORS) | **BROWSER-ENFORCED** |
| `mode: "no-cors"` request | opaque response; the session cookie was **not sent** (SameSite=Lax, cross-site) | **BROWSER-ENFORCED** |
| read another preview (`fetch` its origin) | blocked | **BROWSER-ENFORCED** |
| iframe Redstone's origin and read it | blocked | **BROWSER-ENFORCED** |
| reach into the embedding page (`window.parent.document`, `.localStorage`) | blocked | **BROWSER-ENFORCED** |
| embedding page reads the preview iframe | blocked | **BROWSER-ENFORCED** |
| leave its own cookies or storage for a sibling preview | sibling sees neither | **BROWSER-ENFORCED** |
| cookie tossing via `Set-Cookie: …; Domain=localhost` | the gateway strips `Domain` | **GATEWAY-ENFORCED** |
| cookie tossing via JavaScript `document.cookie = "…; domain=localhost"` | not received by the sibling in Chrome | **BROWSER-ENFORCED** (Chrome only; other browsers **ENVIRONMENT LIMITATION**, untested) |
| `postMessage` to the embedder | delivered, stamped with the preview's origin | **TESTED** — the future frontend **must** check `event.origin` and never send secrets to a preview |
| embedding by an origin not in `frame-ancestors` | blocked | **BROWSER-ENFORCED** via the gateway's CSP |

**Rules this places on the future Redstone frontend and auth — NOT ENFORCED
yet, because they don't exist:**
- session cookies must be `HttpOnly; Secure; SameSite=Lax` (or `Strict`),
  with CSRF protection;
- no CORS allowance for preview origins;
- `postMessage` handlers must validate `event.origin`;
- the UI must use `Referrer-Policy: no-referrer` so preview URLs don't leak.

## Gateway model

**Upstream selection — ENFORCED + GATEWAY-ENFORCED:**
- The gateway reads `Host` only to learn *which preview id is being asked for*.
  It must match `pv[0-9a-f]{32}.<configured domain>[:port]` exactly; anything
  else is a 404.
- The upstream — loopback, the relay's port and its token — comes from
  `PreviewManager`'s server-side map. No header, path or query can name a
  host, port or URL.
- `X-Forwarded-Host`, `X-Forwarded-*`, `Forwarded` and `X-Real-IP` are ignored
  and stripped.
- The gateway never uses host proxy settings, and sends through a bare HTTP
  transport with no redirect, cookie or auth machinery.

| Browser input | Result |
|---|---|
| `Host: internal-redstone-service`, `127.0.0.1`, `localhost`, an unissued or malformed id, the id under another domain | 404 |
| `Host: A` + `X-Forwarded-Host: B` (and friends) | served by A; the app never sees the headers |
| A's origin with B's id in the path or query | served by A |
| `/http://127.0.0.1:8000/`, `/http://169.254.169.254/…`, `/http://10.0.0.1/`, `/http://[::1]/` | delivered to A's own app as a literal path; nothing else contacted |
| `../`, `%2e%2e`, `%252e%252e`, `%2f`, `%5c`, `\`, `%00`, `//x`, control characters | 400; nothing forwarded |
| methods outside `GET HEAD POST PUT PATCH DELETE OPTIONS` | 405 |
| WebSocket upgrade | refused by gateway (policy close) **and** relay (501) |

**Responses — GATEWAY-ENFORCED:**
- `Server`, `X-Powered-By`, `Via` and Redstone-internal headers are dropped,
  including the gateway's own `Server: uvicorn`.
- `Set-Cookie` loses any `Domain=` attribute.
- `Location` must stay on the preview's own origin:
  - relative locations pass;
  - `http://localhost:5173/…` (the app's view of itself) is rewritten to a
    relative path;
  - everything else is refused with 502 — other hosts, `javascript:`,
    `data:`, protocol-relative, and unparsable values.
- Security headers are added: `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Content-Security-Policy: frame-ancestors
  <configured>`, `Cross-Origin-Opener-Policy: same-origin`.
- Errors are generic text and never mention containers, ports or paths.

**Robustness found while testing.** Two hostile `Location` values once crashed
the request handler as a 500: `javascript:…` crashed httpx's redirect
handling, and `http://[::1/%zz` crashed `urlsplit`. Both are fixed, and any
unexpected upstream failure is now a 502 that releases its concurrency slot.

## Preview lifecycle

```
CREATED → STARTING → READY → STOPPED        (stop: runtime destroyed, record kept)
             │         └──→ FAILED           (crash, OOM, sweep)
             └──────────→ FAILED             (startup / readiness failure)
any → DESTROYED                             (destroy: runtime gone, record forgotten)
```

- **Readiness — TESTED.** A preview is READY only when the app answers HTTP
  **end to end through the relay** (`probe_upstream`), with a bounded timeout
  (default 45 s) and bounded polling. The runtime's own health check has also
  passed by then.
- **Identity — ENFORCED.** Every start issues a **new** 128-bit id, so a stopped
  or destroyed preview's URL never reaches a later one (tested). Ids are never
  reused or predictable.
- **Idempotency.** `start` on a READY preview returns it unchanged. `stop` and
  `destroy` are safe to repeat; a second `destroy` returns `False`. `stop` and
  `destroy` wait for an in-flight `start` to finish, then tear down — tested
  mid-startup, with nothing left behind.
- **Reconcile.** Previews live in memory. After a Redstone restart every
  preview URL is dead, and Phase 5.1 reconciliation destroys the app, the
  relay (it carries the runtime's labels) and both networks (tested).
- **Events** carry project id, status and error code only — never the preview
  id (a capability), an address, a port or a token:
  - emitted: `preview.created`, `.starting`, `.ready`, `.failed`, `.stopped`,
    `.destroyed`;
  - **RESERVED**, not emitted: `preview.started`.

## Resource limits

| Limit | Value | How |
|---|---|---|
| CPU / memory / PIDs / output | Phase 4 `ResourceLimits` | **DOCKER-ENFORCED** (fork bomb held under 128; OOM kills only the preview — tested) |
| project storage | `storage_mb` | **PERIODICALLY ENFORCED** (Phase 4.1 watchdog) |
| startup / readiness | 60 s runtime, 45 s end-to-end | **ENFORCED** |
| concurrent previews | 4 | **ENFORCED** (`PREVIEW_LIMIT_REACHED`) |
| idle timeout / max lifetime | 30 min / 4 h | **PERIODICALLY ENFORCED** by the sweeper (`serve.py` starts it) |
| request body | 1 MiB | **GATEWAY-ENFORCED** (413) |
| concurrent requests per preview | 32 | **GATEWAY-ENFORCED** (429) |
| time to first byte / whole response | 30 s / 120 s | **GATEWAY-ENFORCED** |
| response bytes | 32 MiB | **GATEWAY-ENFORCED** (and relay-enforced) |
| WebSockets | 0 | **GATEWAY-ENFORCED**: unsupported |

## WebSockets

**Explicitly unsupported.** The gateway and the relay both refuse upgrades, so
no tunnel of any kind exists.

The consequence is that Vite's hot-module-reload push doesn't work: its client
logs a failed socket connection. The page still loads, and edits are served on
the next request. `test_a_real_vite_app_is_previewable_and_picks_up_edits`
proves a real Vite app picks up an edit with no restart.

Adding HMR later would need identity-checked, bounded, fixed-upstream WebSocket
proxying — **RESERVED**.

## Filesystem

- The project mount is **writable**, because Vite writes its dependency cache
  to `node_modules/.vite`. It's bounded by the storage quota.
- The app can't read host files, `/etc/shadow`, the Docker socket, the
  workspace's sibling `outside-secret.txt`, or anything through a symlink to
  outside, and can't write outside the mount (**DOCKER-ENFORCED**, probed by
  the hostile fixture).
- Vite's own `/@fs/` route only sees the container's filesystem (tested).
- **Windows-host note:** bind-mount file events don't reach the container, so
  a project's file watcher must poll (the Vite fixture sets
  `server.watch.usePolling`), or the preview must be restarted.

## Ports

- The app must listen on **5173** — Redstone's command passes
  `--port 5173 --strictPort`, and the relay forwards only there. An app that
  listens elsewhere never becomes READY and is torn down (tested).
- Nothing the project configures can make Redstone connect to another host or
  port. Other ports the app opens are unreachable: nothing else is on its
  network except the relay, which forwards only 5173.
- The only published port is the relay's, on loopback, with a random host
  port.

## Hostile application — observed results

The fixture `tests/redstone/fixtures/preview_app` is probed from real Docker:

| Attempt | Result |
|---|---|
| secret-like env vars, host-derived env vars | none present |
| `/etc/shadow`, sibling host file, Docker socket (read + exists) | denied / absent |
| write `/etc`, `/workspace` | denied |
| symlink to outside the mount | resolves inside the container only |
| 1.1.1.1, 8.8.8.8, 10/8, 172.16/12, 192.168/16, Docker gateway, 169.254.169.254 | no route |
| `host.docker.internal` (the gateway, and another preview's relay) | no route |
| another preview's app and relay, by container name | unreachable |
| external DNS | not resolved |
| fork 400 processes | < 128 alive |
| exhaust memory | preview OOM-killed; host, gateway and other previews unaffected |
| crash | noticed by the sweeper → FAILED → 503 |

## API

```
POST   /api/projects/{project_id}/preview        start (or return READY); 200 {preview_id, status, url, …}
GET    /api/projects/{project_id}/preview
POST   /api/projects/{project_id}/preview/stop
DELETE /api/projects/{project_id}/preview        {"destroyed": true|false}
```

- The workspace is resolved server-side.
- No request field names a host, port, URL, runtime or path.
- Responses never include the relay port, the token, container ids or host
  paths.
- Errors: `PREVIEW_NOT_FOUND` 404, `PREVIEW_BUSY` 409,
  `PREVIEW_LIMIT_REACHED` 429, `PREVIEW_START_FAILED` 502,
  `PREVIEW_UNAVAILABLE` 503.
- Previews are refused (`PREVIEW_UNAVAILABLE`) on a provider that doesn't
  isolate: `LocalProcessSandboxProvider` is never used to serve untrusted code
  to a browser.
- `python -m redstone.api.serve` runs the API (port 8000) and the gateway
  (port 8100) as two listeners in one process, both on loopback, sharing one
  `PreviewManager`.

## Configuration

Values are validated; invalid input falls back to the default.

| Variable | Default | Notes |
|---|---|---|
| `REDSTONE_PREVIEW_DOMAIN` | `localhost` | must be a host that is not Redstone's; see Browser-origin model |
| `REDSTONE_PREVIEW_SCHEME` | `http` | `http` / `https` |
| `REDSTONE_PREVIEW_PUBLIC_PORT` | 8100 | port shown in URLs; `0` = scheme default |
| `REDSTONE_PREVIEW_LISTEN_PORT` | 8100 | gateway listener (loopback) |
| `REDSTONE_PREVIEW_MAX_ACTIVE` | 4 | 1–32 |
| `REDSTONE_PREVIEW_IDLE_TIMEOUT_SECONDS` | 1800 | 60–86400 |
| `REDSTONE_PREVIEW_FRAME_ANCESTORS` | `'self'` | `'none'`, `'self'`, or explicit `http(s)://host[:port]` origins only |

The default `frame-ancestors 'self'` means **the future Redstone UI can't embed
previews until the operator sets this to the UI's origin.** That is
deliberate: embedding is opt-in.

## Testing

| File | Tests | Kind |
|---|---|---|
| `test_preview.py` | 53 | **REAL DOCKER** + real sockets (two hostile previews) |
| `test_preview_lifecycle.py` | 14 | **REAL DOCKER** (incl. real Vite, and agent → API → preview) |
| `test_preview_browser.py` | 3 | **REAL BROWSER** (system Chrome via Playwright) + **REAL DOCKER** |
| `test_preview_gateway.py` | 79 | UNIT (policy functions, ids, config) + MOCKED (fake provider, stand-in relay on real sockets) |

- The browser tests skip with an explicit reason if Playwright isn't
  installed. In this environment it was installed into the job's temp
  directory, not globally, and all three ran.
- Real-Docker tests skip with an explicit reason when no daemon is reachable.

## Remaining limitations

- **Authorization is capability-based only — NOT ENFORCED as user auth.**
  - Anyone holding a preview URL can view that preview; Redstone has no user
    authentication yet.
  - The 128-bit id in the hostname is unguessable, but it appears in browser
    history and — in production — in DNS queries, and in certificate
    transparency logs unless a wildcard certificate is used.
  - The Redstone API itself is also unauthenticated (pre-existing).
- **Production domain isolation is a deployment requirement:** a separate,
  ideally PSL-listed, registrable domain. Code can't enforce it.
- **Browser coverage:** Chrome only. Firefox and Safari are **ENVIRONMENT
  LIMITATION**, untested.
- **No WebSockets / HMR** (see above).
- **The relay and the gateway are trusted parsers of hostile HTTP.** Two gateway
  crash paths were found and fixed during this phase; others may exist. Every
  unexpected upstream failure is a 502, never a hang or a leak. A compromised
  relay would gain its bridge network's internet egress while still non-root,
  capability-less and confined.
- **The loopback-published relay port is reachable by local processes and by
  containers** via `host.docker.internal`; the token is what protects it
  (tested).
- **`stop` / `destroy` wait for an in-flight start**, up to the readiness
  timeout.
- **Previews don't survive a Redstone restart** (by design; reconciled).
- **The idle/lifetime sweeper runs only if started** (`serve.py` does); crash
  detection is periodic, not instant.
- **Project creation through the API still defaults to the `static`
  framework**, which has no dev-server command. Preview needs a
  `react-vite-ts` project (pre-existing API limitation).
