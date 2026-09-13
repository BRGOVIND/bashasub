// Adversarial postinstall probe (test fixture only).
//
// Every probe is non-destructive: it attempts an access and records the
// outcome. Results go to probe-results.json in the project root (the one
// place a sandboxed process is allowed to write), which the test reads back
// from the host. The script always exits 0 so npm install itself succeeds --
// the point is to observe what the lifecycle script could reach, not to
// fail the install.
"use strict";

const fs = require("fs");
const net = require("net");
const path = require("path");
const { spawn } = require("child_process");

const results = { uid: process.getuid(), gid: process.getgid() };

// 1. Secrets in the environment.
const secretish = /(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|DATABASE_URL|^REDSTONE_|^AI_)/i;
results.env_keys = Object.keys(process.env).sort();
results.secret_like_env = results.env_keys.filter((k) => secretish.test(k));

// 2. Reading outside the project.
function tryRead(p) {
  try {
    const data = fs.readFileSync(p, "utf8");
    return { ok: true, bytes: data.length, head: data.slice(0, 64) };
  } catch (e) {
    return { ok: false, code: e.code };
  }
}
results.read = {
  etc_shadow: tryRead("/etc/shadow"),
  host_secret_path: tryRead(process.env.PROBE_OUTSIDE_FILE || "/nonexistent"),
  parent_of_mount: tryRead(path.resolve("/workspace/project/../outside-secret.txt")),
  proc1_environ: tryRead("/proc/1/environ"),
};

// 3. Writing outside the project.
function tryWrite(p) {
  try {
    fs.writeFileSync(p, "pwned");
    return { ok: true };
  } catch (e) {
    return { ok: false, code: e.code };
  }
}
results.write = {
  etc: tryWrite("/etc/redstone-pwned"),
  usr_local: tryWrite("/usr/local/lib/redstone-pwned"),
  workspace_parent: tryWrite("/workspace/redstone-pwned"),
  tmp: tryWrite("/tmp/redstone-probe"),   // allowed: bounded tmpfs
};

// 4. Network.
function tryConnect(host, port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host, port });
    const done = (outcome) => {
      socket.destroy();
      resolve(outcome);
    };
    socket.setTimeout(3000, () => done({ ok: false, code: "TIMEOUT" }));
    socket.on("connect", () => done({ ok: true }));
    socket.on("error", (e) => done({ ok: false, code: e.code }));
  });
}

// 5. Process exhaustion (bounded attempt: stops at the first failure).
function trySpawnMany(n) {
  return new Promise((resolve) => {
    let spawned = 0;
    let failed = null;
    const children = [];
    for (let i = 0; i < n; i++) {
      try {
        const child = spawn("sleep", ["5"]);
        child.on("error", (e) => { if (!failed) failed = e.code; });
        children.push(child);
        spawned += 1;
      } catch (e) {
        failed = e.code || String(e);
        break;
      }
    }
    setTimeout(() => {
      const alive = children.filter((c) => c.pid && c.exitCode === null).length;
      children.forEach((c) => { try { c.kill("SIGKILL"); } catch (_) {} });
      resolve({ attempted: n, spawned, alive, failed });
    }, 1000);
  });
}

// 6. The egress proxy itself (Phase 4.2): ask it for forbidden destinations.
function tryProxyConnect(target) {
  return new Promise((resolve) => {
    let proxy;
    try { proxy = new URL(process.env.HTTPS_PROXY || process.env.https_proxy); }
    catch (_) { resolve({ status: "NO_PROXY_CONFIGURED" }); return; }
    const socket = net.connect({ host: proxy.hostname, port: Number(proxy.port) });
    const done = (status) => { socket.destroy(); resolve({ status }); };
    socket.setTimeout(10000, () => done("TIMEOUT"));
    socket.on("connect", () => socket.write(`CONNECT ${target} HTTP/1.1\r\nHost: ${target}\r\n\r\n`));
    socket.on("data", (d) => done(String(d).split("\r\n")[0]));
    socket.on("error", (e) => done(`ERR ${e.code}`));
  });
}

// 7. npm itself, with every knob an attacker controls.
const { spawnSync } = require("child_process");
const PROXY_VARS = ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                    "npm_config_proxy", "npm_config_https_proxy"];
function npmRun(args, envPatch = {}, unset = []) {
  const env = { ...process.env, ...envPatch };
  for (const key of unset) delete env[key];
  const run = spawnSync("npm", [...args, "--fetch-retries=0", "--fetch-timeout=8000"],
                        { env, timeout: 60000, encoding: "utf8" });
  return { status: run.status, signal: run.signal };
}

(async () => {
  results.net = {};
  if (process.env.PROBE_TARGET_IP) {
    results.net.other_container = await tryConnect(process.env.PROBE_TARGET_IP, 8080);
  }
  results.net.metadata = await tryConnect("169.254.169.254", 80);
  results.net.localhost = await tryConnect("127.0.0.1", 3128);
  results.net.private_ip = await tryConnect("10.0.0.1", 443);
  results.net.public_ip_direct = await tryConnect("1.1.1.1", 443);
  if (process.env.PROBE_REGISTRY_IP) {
    results.net.registry_ip_direct = await tryConnect(process.env.PROBE_REGISTRY_IP, 443);
  }
  results.net.proxy_connect_example = await tryProxyConnect("example.com:443");
  results.net.proxy_connect_metadata = await tryProxyConnect("169.254.169.254:80");
  results.net.proxy_connect_registry_ip = await tryProxyConnect(
    `${process.env.PROBE_REGISTRY_IP || "104.16.0.1"}:443`);

  results.npm = {
    no_proxy_other_registry: npmRun(
      ["view", "is-odd", "version", "--registry=https://registry.yarnpkg.com/"],
      { NO_PROXY: "*", no_proxy: "*" }),
    unset_proxy_vars: npmRun(["view", "is-odd", "version"], {}, PROXY_VARS),
    attacker_proxy: npmRun(["view", "is-odd", "version"],
      Object.fromEntries(PROXY_VARS.map((k) => [k, "http://1.1.1.1:8080"]))),
    arbitrary_registry_url: npmRun(["view", "is-odd", "version", "--registry=https://example.com/"]),
  };

  results.processes = await trySpawnMany(400);

  const out = "/workspace/project/probe-results.json";
  fs.writeFileSync(out, JSON.stringify(results, null, 2));
  console.log("PROBE_COMPLETE");
  process.exit(0);
})();
