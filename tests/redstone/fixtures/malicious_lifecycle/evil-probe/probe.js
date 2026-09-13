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

(async () => {
  results.net = {};
  if (process.env.PROBE_TARGET_IP) {
    results.net.other_container = await tryConnect(process.env.PROBE_TARGET_IP, 8080);
  }
  results.net.metadata = await tryConnect("169.254.169.254", 80);
  results.processes = await trySpawnMany(400);

  const out = "/workspace/project/probe-results.json";
  fs.writeFileSync(out, JSON.stringify(results, null, 2));
  console.log("PROBE_COMPLETE");
  process.exit(0);
})();
