// Redstone preview relay (Phase 6).
//
// One per preview. The preview app sits alone on a Docker --internal
// network with this relay; the relay is also on a small bridge network
// whose single published port is bound to host LOOPBACK. That published
// port is reachable from the host and -- on Docker Desktop, verified -- from
// other containers via host.docker.internal, so it is never trusted by
// position: every request must carry this preview's random 256-bit token,
// which only Redstone's preview gateway holds.
//
// Policy (all of it):
//   * token required (constant-time compare), else 403;
//   * upstream is FIXED at startup (this preview's own container, port
//     5173) -- nothing in a request can select another host or port;
//   * origin-form paths only; standard HTTP methods only; CONNECT and
//     WebSocket upgrades refused (no tunnels);
//   * Host is rewritten to localhost:5173 (the only host a Vite dev server
//     accepts), forwarding headers and the token are stripped;
//   * bounded: concurrent requests, header/request time, body and
//     response bytes;
//   * logs denials only -- never headers, paths or bodies.
"use strict";

const http = require("http");
const crypto = require("crypto");

const LISTEN_PORT = 8080;
const UPSTREAM_PORT = 5173;
const TOKEN_HEADER = "x-redstone-relay-token";
const STATUS_HEADER = "x-redstone-relay";
const MAX_BODY_BYTES = 1024 * 1024;
const MAX_RESPONSE_BYTES = 32 * 1024 * 1024;
const UPSTREAM_TIMEOUT_MS = 30000;
const MAX_CONCURRENT = 64;
const METHODS = new Set(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]);
const DROP_REQUEST = new Set([
  "connection", "keep-alive", "proxy-authorization", "proxy-connection", "te", "trailer",
  "transfer-encoding", "upgrade", "host", "forwarded", "x-real-ip", "via", TOKEN_HEADER,
]);
const DROP_RESPONSE = new Set(["connection", "keep-alive", "transfer-encoding", STATUS_HEADER]);

function log(record) {
  record.ts = new Date().toISOString();
  process.stdout.write(JSON.stringify(record) + "\n");
}

function fail(reason) {
  log({ event: "startup_failed", reason });
  process.exit(2);
}

const TOKEN = process.env.REDSTONE_RELAY_TOKEN || "";
const UPSTREAM = process.env.REDSTONE_RELAY_UPSTREAM || "";
if (!/^[0-9a-f]{64}$/.test(TOKEN)) fail("invalid token");
if (!/^redstone-[0-9a-f]{16}$/.test(UPSTREAM)) fail("invalid upstream");
const TOKEN_BYTES = Buffer.from(TOKEN);

function authorised(req) {
  const presented = req.headers[TOKEN_HEADER];
  if (typeof presented !== "string") return false;
  const bytes = Buffer.from(presented);
  return bytes.length === TOKEN_BYTES.length && crypto.timingSafeEqual(bytes, TOKEN_BYTES);
}

function deny(res, status, reason) {
  log({ event: "request", decision: "deny", reason });
  res.writeHead(status, { "content-type": "text/plain", [STATUS_HEADER]: "denied" });
  res.end();
}

let active = 0;

const server = http.createServer((req, res) => {
  if (!authorised(req)) return deny(res, 403, "token");
  if (!METHODS.has(req.method)) return deny(res, 405, "method");
  if (typeof req.url !== "string" || !req.url.startsWith("/") || req.url.startsWith("//")
      || req.url.length > 8192) {
    return deny(res, 400, "path");
  }
  if (active >= MAX_CONCURRENT) return deny(res, 503, "busy");

  active += 1;
  let released = false;
  const release = () => { if (!released) { released = true; active -= 1; } };
  res.on("close", release);

  const headers = {};
  for (const [name, value] of Object.entries(req.headers)) {
    const key = name.toLowerCase();
    if (DROP_REQUEST.has(key) || key.startsWith("x-forwarded-") || key.startsWith("x-redstone-")) continue;
    headers[key] = value;
  }
  headers.host = `localhost:${UPSTREAM_PORT}`;

  const upstream = http.request({
    host: UPSTREAM, port: UPSTREAM_PORT, method: req.method, path: req.url, headers,
    timeout: UPSTREAM_TIMEOUT_MS,
  }, (up) => {
    const out = {};
    for (const [name, value] of Object.entries(up.headers)) {
      if (!DROP_RESPONSE.has(name.toLowerCase())) out[name] = value;
    }
    res.writeHead(up.statusCode || 502, out);
    let bytes = 0;
    up.on("data", (chunk) => {
      bytes += chunk.length;
      if (bytes > MAX_RESPONSE_BYTES) {
        log({ event: "response", decision: "closed", reason: "response_too_large" });
        up.destroy();
        res.destroy();
      }
    });
    up.pipe(res);
  });
  upstream.on("timeout", () => upstream.destroy(new Error("timeout")));
  upstream.on("error", () => {
    if (!res.headersSent) {
      res.writeHead(502, { "content-type": "text/plain", [STATUS_HEADER]: "upstream-unreachable" });
      res.end();
    } else {
      res.destroy();
    }
  });

  let inBytes = 0;
  req.on("data", (chunk) => {
    inBytes += chunk.length;
    if (inBytes > MAX_BODY_BYTES) {
      upstream.destroy();
      if (!res.headersSent) deny(res, 413, "body_too_large");
    }
  });
  req.pipe(upstream);
});

// No tunnels of any kind.
server.on("upgrade", (req, socket) => {
  log({ event: "upgrade", decision: "deny", reason: "websocket_unsupported" });
  socket.end("HTTP/1.1 501 Not Implemented\r\nConnection: close\r\n\r\n");
});
server.on("connect", (req, socket) => {
  log({ event: "connect", decision: "deny", reason: "connect_unsupported" });
  socket.end("HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n");
});
server.headersTimeout = 10000;
server.requestTimeout = 60000;
server.maxHeadersCount = 100;

server.listen(LISTEN_PORT, "0.0.0.0", () => {
  log({ event: "ready" });
  process.stdout.write("REDSTONE_PREVIEW_RELAY_READY\n");
});

for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => { server.close(); process.exit(0); });
}
