// Redstone install egress proxy.
//
// Runs as its own hardened container, the ONLY peer an install sandbox can
// reach: the sandbox sits on a Docker --internal network (no route out at
// all), and this process is the one container attached both to that network
// and to an outbound network. So the sandbox's network access is exactly
// what this file allows, and nothing it does to its own proxy variables,
// npm config or DNS can widen that.
//
// Policy (all of it; there is deliberately nothing else):
//   * only `CONNECT host:port` -- no plain-HTTP forwarding, no absolute-URI
//     requests, so this process never fetches, never follows a redirect, and
//     never sees inside TLS. A redirect to another host means a new CONNECT,
//     which is checked like any other.
//   * host must EXACTLY match the allowlist (case-insensitive, one trailing
//     dot tolerated). IP literals, IPv6 brackets, userinfo, wildcards and
//     anything that is not a plain DNS name are refused before any lookup.
//   * port must be in the allowed port list.
//   * every address the name resolves to must be public unicast; one
//     loopback/private/link-local/reserved answer denies the whole request.
//   * the upstream socket connects to the exact address that was validated,
//     never to the name again -- no second resolution to race.
//   * bounded: header bytes, concurrent tunnels, connect/idle timeouts,
//     tunnel lifetime and bytes per tunnel.
//   * logs decisions only (host, port, decision, reason). Never headers,
//     so a Proxy-Authorization or anything else a client sends is not
//     recorded.
"use strict";

const net = require("net");
const dns = require("dns");

const LISTEN_PORT = 3128;
const MAX_HEADER_BYTES = 8192;
const HEADER_TIMEOUT_MS = 10000;
const HOST_RE = /^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/;

function fail(message) {
  log({ event: "startup_failed", reason: message });
  process.exit(2);
}

function intEnv(name, fallback, min, max) {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < min || value > max) fail(`invalid ${name}`);
  return value;
}

function log(record) {
  record.ts = new Date().toISOString();
  if (typeof record.host === "string") record.host = record.host.slice(0, 253);
  process.stdout.write(JSON.stringify(record) + "\n");
}

// ------------------------------------------------------------------ config

const allowedHosts = new Set(
  (process.env.REDSTONE_EGRESS_ALLOW || "")
    .split(",").map((h) => h.trim().toLowerCase()).filter(Boolean),
);
if (allowedHosts.size === 0) fail("empty allowlist");
// The top-level label must contain a letter: inet_aton-style numeric names
// ("127.1", "0x7f.1") would otherwise resolve to arbitrary addresses.
const hasAlphaTld = (host) => /[a-z]/.test(host.split(".").pop());
for (const host of allowedHosts) {
  if (!HOST_RE.test(host) || net.isIP(host) || !hasAlphaTld(host)) fail("invalid allowlist entry");
}
const allowedPorts = new Set(
  (process.env.REDSTONE_EGRESS_PORTS || "443").split(",").map((p) => Number(p.trim())),
);
for (const port of allowedPorts) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) fail("invalid port");
}
const CONNECT_TIMEOUT_MS = intEnv("REDSTONE_EGRESS_CONNECT_TIMEOUT_MS", 10000, 500, 120000);
const IDLE_TIMEOUT_MS = intEnv("REDSTONE_EGRESS_IDLE_TIMEOUT_MS", 60000, 1000, 600000);
const MAX_TUNNEL_MS = intEnv("REDSTONE_EGRESS_MAX_TUNNEL_MS", 600000, 1000, 3600000);
const MAX_TUNNEL_BYTES = intEnv("REDSTONE_EGRESS_MAX_TUNNEL_BYTES", 512 * 1024 * 1024, 1024, 4 * 1024 * 1024 * 1024);
const MAX_CONNECTIONS = intEnv("REDSTONE_EGRESS_MAX_CONNECTIONS", 32, 1, 1024);

// ------------------------------------------------------ address classifier

function ipv4ToInt(ip) {
  return ip.split(".").reduce((acc, octet) => (acc << 8) + Number(octet), 0) >>> 0;
}

const IPV4_DENY = [
  ["0.0.0.0", 8], ["10.0.0.0", 8], ["100.64.0.0", 10], ["127.0.0.0", 8],
  ["169.254.0.0", 16], ["172.16.0.0", 12], ["192.0.0.0", 24], ["192.0.2.0", 24],
  ["192.88.99.0", 24], ["192.168.0.0", 16], ["198.18.0.0", 15], ["198.51.100.0", 24],
  ["203.0.113.0", 24], ["224.0.0.0", 4], ["240.0.0.0", 4],
].map(([base, bits]) => [ipv4ToInt(base), bits === 0 ? 0 : (~0 << (32 - bits)) >>> 0]);

function isPublicIPv4(ip) {
  const value = ipv4ToInt(ip);
  return !IPV4_DENY.some(([base, mask]) => ((value & mask) >>> 0) === ((base & mask) >>> 0));
}

function expandIPv6(ip) {
  let [head, tail] = ip.split("::");
  const headParts = head ? head.split(":") : [];
  let tailParts = tail !== undefined && tail !== "" ? tail.split(":") : [];
  // Embedded IPv4 (e.g. ::ffff:1.2.3.4) -> two hextets.
  const last = (tail !== undefined ? tailParts : headParts);
  if (last.length && last[last.length - 1].includes(".")) {
    const v4 = ipv4ToInt(last.pop());
    last.push((v4 >>> 16).toString(16), (v4 & 0xffff).toString(16));
  }
  const fill = tail !== undefined ? 8 - headParts.length - tailParts.length : 0;
  const parts = [...headParts, ...Array(fill).fill("0"), ...tailParts];
  if (parts.length !== 8) return null;
  return parts.map((p) => parseInt(p, 16));
}

function isPublicIPv6(ip) {
  const h = expandIPv6(ip.toLowerCase().split("%")[0]);
  if (!h || h.some((x) => Number.isNaN(x))) return false;
  // IPv4-mapped / translated forms: judge the embedded IPv4, conservatively.
  if (h.slice(0, 5).every((x) => x === 0) && h[5] === 0xffff) {
    return isPublicIPv4(`${h[6] >> 8}.${h[6] & 255}.${h[7] >> 8}.${h[7] & 255}`);
  }
  if ((h[0] & 0xe000) !== 0x2000) return false;          // only 2000::/3 global unicast
  if (h[0] === 0x2001 && h[1] < 0x0200) return false;    // 2001::/23 IETF special-purpose (incl. Teredo)
  if (h[0] === 0x2001 && h[1] === 0x0db8) return false;  // documentation
  if (h[0] === 0x2002) return false;                     // 6to4 can embed private IPv4
  return true;
}

function isPublicAddress(ip) {
  const family = net.isIP(ip);
  if (family === 4) return isPublicIPv4(ip);
  if (family === 6) return isPublicIPv6(ip);
  return false;
}

// ------------------------------------------------------------------ policy

function normaliseHost(raw) {
  if (typeof raw !== "string") return null;
  let host = raw.toLowerCase();
  if (host.endsWith(".")) host = host.slice(0, -1);
  if (!HOST_RE.test(host) || net.isIP(host) || !hasAlphaTld(host)) return null;
  return host;
}

function parseTarget(target) {
  // Accept exactly "name:port". No brackets, no userinfo, no path.
  const match = /^([^:\s/@\[\]#?]+):(\d{1,5})$/.exec(target || "");
  if (!match) return { error: "malformed_target" };
  if (net.isIP(match[1])) return { error: "ip_literal" };
  const host = normaliseHost(match[1]);
  if (!host) return { error: "invalid_host" };
  const port = Number(match[2]);
  return { host, port };
}

// ------------------------------------------------------------------ server

let active = 0;

function reply(socket, status, text) {
  if (!socket.destroyed) {
    socket.end(`HTTP/1.1 ${status} ${text}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n`);
  }
}

function deny(socket, host, port, reason, status = 403, text = "Forbidden") {
  log({ event: "connect", host, port, decision: "deny", reason });
  reply(socket, status, text);
}

function tunnel(client, host, port, address, leftover) {
  const upstream = net.connect({ host: address, port });
  let bytes = 0;
  let established = false;

  const close = () => { client.destroy(); upstream.destroy(); };
  const lifetime = setTimeout(close, MAX_TUNNEL_MS);
  const connectTimer = setTimeout(() => {
    if (!established) {
      log({ event: "connect", host, port, decision: "error", reason: "upstream_timeout" });
      reply(client, 504, "Gateway Timeout");
      upstream.destroy();
    }
  }, CONNECT_TIMEOUT_MS);

  const count = (chunk) => {
    bytes += chunk.length;
    if (bytes > MAX_TUNNEL_BYTES) {
      log({ event: "tunnel", host, port, decision: "closed", reason: "byte_limit" });
      close();
    }
  };

  upstream.on("connect", () => {
    established = true;
    clearTimeout(connectTimer);
    log({ event: "connect", host, port, decision: "allow", address });
    client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
    if (leftover.length) { count(leftover); upstream.write(leftover); }
    client.on("data", count);
    upstream.on("data", count);
    client.pipe(upstream);
    upstream.pipe(client);
  });
  upstream.on("error", () => {
    if (!established) {
      clearTimeout(connectTimer);
      log({ event: "connect", host, port, decision: "error", reason: "upstream_unreachable" });
      reply(client, 502, "Bad Gateway");
    }
    close();
  });
  for (const socket of [client, upstream]) {
    socket.setTimeout(IDLE_TIMEOUT_MS, close);
    socket.on("close", () => { clearTimeout(lifetime); clearTimeout(connectTimer); close(); });
  }
}

function handle(client) {
  if (active >= MAX_CONNECTIONS) {
    log({ event: "connect", decision: "deny", reason: "too_many_connections" });
    reply(client, 503, "Service Unavailable");
    return;
  }
  active += 1;
  client.on("close", () => { active -= 1; });
  client.on("error", () => client.destroy());

  let buffer = Buffer.alloc(0);
  const headerTimer = setTimeout(() => client.destroy(), HEADER_TIMEOUT_MS);

  const onData = (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    const end = buffer.indexOf("\r\n\r\n");
    if (end === -1) {
      if (buffer.length > MAX_HEADER_BYTES) {
        clearTimeout(headerTimer);
        client.removeListener("data", onData);
        deny(client, undefined, undefined, "header_too_large", 431, "Request Header Fields Too Large");
      }
      return;
    }
    clearTimeout(headerTimer);
    client.removeListener("data", onData);
    client.pause();
    if (end > MAX_HEADER_BYTES) {
      deny(client, undefined, undefined, "header_too_large", 431, "Request Header Fields Too Large");
      return;
    }
    const requestLine = buffer.slice(0, buffer.indexOf("\r\n")).toString("latin1");
    const leftover = buffer.slice(end + 4);
    const parts = requestLine.split(" ");

    if (parts.length === 3 && parts[0] === "GET" && parts[1] === "/__redstone_health"
        && (client.remoteAddress === "127.0.0.1" || client.remoteAddress === "::ffff:127.0.0.1")) {
      reply(client, 200, "OK");
      return;
    }
    if (parts.length !== 3 || parts[0] !== "CONNECT") {
      deny(client, undefined, undefined, "method_not_allowed", 405, "Method Not Allowed");
      return;
    }

    const target = parseTarget(parts[1]);
    if (target.error) { deny(client, parts[1], undefined, target.error); return; }
    const { host, port } = target;
    if (!allowedHosts.has(host)) { deny(client, host, port, "host_not_allowed"); return; }
    if (!allowedPorts.has(port)) { deny(client, host, port, "port_not_allowed"); return; }

    dns.lookup(host, { all: true, verbatim: true }, (err, addresses) => {
      if (client.destroyed) return;
      if (err || !addresses || addresses.length === 0) {
        deny(client, host, port, "dns_failure", 502, "Bad Gateway");
        return;
      }
      const forbidden = addresses.find((a) => !isPublicAddress(a.address));
      if (forbidden) {
        deny(client, host, port, "resolves_to_forbidden_address");
        return;
      }
      const chosen = addresses.find((a) => a.family === 4) || addresses[0];
      // The client stays paused until the upstream connects: pipe() resumes
      // it, so no early bytes are emitted with nothing listening.
      tunnel(client, host, port, chosen.address, leftover);
    });
  };
  client.on("data", onData);
}

const server = net.createServer(handle);
server.maxConnections = MAX_CONNECTIONS * 2;
server.on("error", (e) => fail(`listen: ${e.code}`));
server.listen(LISTEN_PORT, "0.0.0.0", () => {
  log({ event: "ready", allow: [...allowedHosts], ports: [...allowedPorts], max_connections: MAX_CONNECTIONS });
  process.stdout.write("REDSTONE_EGRESS_PROXY_READY\n");
});

for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => {
    server.close();
    process.exit(0);
  });
}

module.exports = { isPublicAddress, parseTarget };
