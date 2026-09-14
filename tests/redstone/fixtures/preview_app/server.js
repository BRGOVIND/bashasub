// Hostile preview app (test fixture only).
//
// It is the "generated project" at its worst: on startup it probes for
// secrets, host files, the Docker socket and every network destination it
// can think of, and its HTTP routes try to subvert the preview gateway
// (spoofed infrastructure headers, cookie tossing, off-origin and
// javascript: redirects, oversized and never-ending responses, fork bombs,
// memory exhaustion, crashing). Every probe is non-destructive to the host:
// it attempts an access and records the outcome.
"use strict";

const http = require("http");
const fs = require("fs");
const net = require("net");
const dns = require("dns");
const path = require("path");
const { spawn } = require("child_process");

const ROOT = __dirname;
const readText = (name, fallback) => {
  try { return fs.readFileSync(path.join(ROOT, name), "utf8").trim(); } catch (_) { return fallback; }
};
const IDENTITY = readText("identity.txt", "unknown");
const PORT = Number(readText("port.txt", "5173"));
let requests = 0;
const probes = { done: false };

function tryRead(p) {
  try { const data = fs.readFileSync(p, "utf8"); return { ok: true, head: data.slice(0, 80) }; }
  catch (e) { return { ok: false, code: e.code }; }
}
function tryWrite(p) {
  try { fs.writeFileSync(p, "pwned"); return { ok: true }; } catch (e) { return { ok: false, code: e.code }; }
}
function tryConnect(host, port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host, port });
    const done = (outcome) => { socket.destroy(); resolve(outcome); };
    socket.setTimeout(3000, () => done({ ok: false, code: "TIMEOUT" }));
    socket.on("connect", () => done({ ok: true }));
    socket.on("error", (e) => done({ ok: false, code: e.code }));
  });
}

async function runProbes() {
  probes.uid = process.getuid();
  probes.env_keys = Object.keys(process.env).sort();
  probes.secret_env = probes.env_keys.filter(
    (k) => /(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|DATABASE_URL|^REDSTONE_|^AI_)/i.test(k));
  probes.read = {
    shadow: tryRead("/etc/shadow"),
    outside_mount: tryRead("/workspace/outside-secret.txt"),
    docker_sock: tryRead("/var/run/docker.sock"),
  };
  probes.docker_sock_exists = fs.existsSync("/var/run/docker.sock") || fs.existsSync("/run/docker.sock");
  probes.write = { etc: tryWrite("/etc/pwned"), workspace_parent: tryWrite("/workspace/pwned") };
  try {
    fs.symlinkSync("/workspace", path.join("/tmp", "up"));
    probes.symlink_outside = tryRead("/tmp/up/outside-secret.txt");
  } catch (e) { probes.symlink_outside = { ok: false, code: e.code }; }

  let extra = [];
  try { extra = JSON.parse(readText("probe-targets.json", "[]")); } catch (_) { /* none */ }
  const targets = [
    ["internet", "1.1.1.1", 443], ["public_dns", "8.8.8.8", 53],
    ["private_10", "10.0.0.1", 80], ["private_172", "172.16.0.1", 80], ["private_192", "192.168.1.1", 80],
    ["docker_gateway", "172.17.0.1", 80], ["metadata", "169.254.169.254", 80],
    ["host_docker_internal", "host.docker.internal", 80],
    ...extra,
  ];
  probes.net = {};
  for (const [name, host, port] of targets) probes.net[name] = await tryConnect(host, Number(port));
  probes.dns = await new Promise((r) => dns.lookup("example.com", (e, a) => r(e ? e.code : `RESOLVED ${a}`)));
  probes.done = true;
}

function send(res, status, body, headers = {}) {
  res.writeHead(status, { "content-type": "text/plain", ...headers });
  res.end(body);
}

const routes = {
  "/__whoami": (req, res) => send(res, 200, IDENTITY),
  "/__count": (req, res) => send(res, 200, String(requests)),
  "/__echo": (req, res) => send(res, 200, JSON.stringify({ method: req.method, url: req.url, headers: req.headers }),
    { "content-type": "application/json" }),
  // Answers at once with whatever has been probed so far; callers poll
  // until `done` (probing every destination takes longer than a request).
  "/__probe": (req, res) => send(res, 200, JSON.stringify(probes), { "content-type": "application/json" }),
  "/__headers": (req, res) => send(res, 200, "headers", {
    "server": "evil-internal-server/1.0", "x-powered-by": "EvilFramework", "via": "1.1 internal-proxy",
    "x-redstone-relay": "upstream-unreachable", "x-custom": "kept",
  }),
  "/__cookie": (req, res) => {
    res.writeHead(200, { "set-cookie": [
      "tossed_by_header=1; Domain=localhost; Path=/",
      "plain=2; Path=/",
      "dotted=3; domain=.example.com; Path=/; HttpOnly",
    ] });
    res.end("cookies");
  },
  "/__redirect/internal": (req, res) => send(res, 302, "", { location: "http://internal-redstone-service/admin" }),
  "/__redirect/metadata": (req, res) => send(res, 302, "", { location: "http://169.254.169.254/latest/meta-data/" }),
  "/__redirect/relative": (req, res) => send(res, 302, "", { location: "/landing" }),
  "/__redirect/self": (req, res) => send(res, 302, "", { location: "http://localhost:5173/landing?x=1" }),
  "/__redirect/js": (req, res) => send(res, 302, "", { location: "javascript:alert(document.cookie)" }),
  "/__redirect/protocol-relative": (req, res) => send(res, 302, "", { location: "//evil.example/" }),
  "/__big": (req, res) => {
    res.writeHead(200, { "content-type": "application/octet-stream" });
    const chunk = Buffer.alloc(1024 * 1024, 120);
    let sent = 0;
    const pump = () => {
      while (sent < 20 && res.write(chunk)) sent += 1;
      if (sent >= 20) res.end(); else res.once("drain", () => { sent += 1; pump(); });
    };
    pump();
  },
  "/__slow": () => { /* never answers */ },
  "/__fork": (req, res) => {
    const children = [];
    for (let i = 0; i < 400; i += 1) {
      try { const c = spawn("sleep", ["5"]); c.on("error", () => {}); children.push(c); } catch (_) { break; }
    }
    setTimeout(() => {
      const alive = children.filter((c) => c.pid && c.exitCode === null).length;
      children.forEach((c) => { try { c.kill("SIGKILL"); } catch (_) { /* gone */ } });
      send(res, 200, JSON.stringify({ attempted: 400, alive }), { "content-type": "application/json" });
    }, 1000);
  },
  "/__alloc": (req, res) => {
    send(res, 200, "allocating");
    const hoard = [];
    setInterval(() => { for (let i = 0; i < 8; i += 1) hoard.push(Buffer.alloc(16 * 1024 * 1024, 1)); }, 20);
  },
  "/__crash": (req, res) => { send(res, 200, "bye"); setTimeout(() => process.exit(1), 100); },
};

const pages = { "/attack.html": "attack.html" };

http.createServer((req, res) => {
  requests += 1;
  const pathname = req.url.split("?")[0];
  if (req.method === "POST" && pathname === "/__body") {
    let n = 0;
    req.on("data", (c) => { n += c.length; });
    req.on("end", () => send(res, 200, String(n)));
    return;
  }
  if (routes[pathname]) return routes[pathname](req, res);
  if (pages[pathname]) {
    return send(res, 200, readText(pages[pathname], ""), { "content-type": "text/html; charset=utf-8" });
  }
  send(res, 200, `<!doctype html><title>${IDENTITY}</title><h1>preview ${IDENTITY}</h1>`,
    { "content-type": "text/html; charset=utf-8" });
}).listen(PORT, "0.0.0.0", () => { runProbes(); });
