# Redstone sandbox

Phase 4, hardened in Phase 4.1, install egress closed in Phase 4.2. A
provider-neutral abstraction (`redstone.sandbox`) for executing one untrusted,
bounded or long-running process — a generated project's dependency install,
its dev server, or a typecheck/lint/build — behind a real security boundary.
`redstone.runtime.RuntimeManager` (see `RUNTIME.md`) and
`SandboxValidationRunner` are the only things above this layer that call it.

**Threat model: assume arbitrary code execution inside the sandbox** —
generated source, every npm package, and every lifecycle script. Every claim
below is labelled:

| Label | Meaning |
|---|---|
| **ENFORCED BY DOCKER** | A kernel/Docker mechanism blocks it (no route, no capability, cgroup limit…), and a real-Docker test drove it and observed the block. |
| **ENFORCED BY PROXY** | Redstone's egress proxy refuses it, and a real-Docker test observed the refusal from inside a real sandbox. |
| **PERIODICALLY ENFORCED** | Redstone measures and kills on a schedule (storage). Real, tested, not instantaneous. |
| **VERIFIED STATE** | The kernel reports the protective state on the real process; no attack was attempted against it. |
| **NOT ENFORCED** | Known open gap, stated here and in `SECURITY.md`. |
| **ENVIRONMENT LIMITATION** | Can't be reproduced on this development machine. |
| **RESERVED** | Declared for later; nothing uses it. |

> **Environment.** Built and tested against a real Docker daemon (Docker
> Desktop 29.4.1, WSL2 backend — a genuine Linux kernel), never assumed.
> Without a daemon, `best_available_provider()` falls back to
> `LocalProcessSandboxProvider`, which isolates **nothing**
> (`is_isolated = False`); `/api/health` says which one is active.

## Architecture

```
SandboxProvider (Protocol, redstone/sandbox/provider.py)
   create(config) -> sandbox_id      prepare; for INSTALL_ONLY also builds the
                                      proxy + networks (fails closed)
   start(sandbox_id)                 run; arms the storage watchdog
   wait(sandbox_id, timeout)         bounded op: exit or whole-tree kill on timeout
   exec_in(sandbox_id, argv, timeout) one probe inside a running sandbox
   status / logs                     incl. resource_limit_exceeded; bounded logs
   stop / kill / destroy             idempotent; destroy removes proxy + networks
   list_managed()                    Redstone-owned sandboxes, read from Docker
```

## Command policy: named operations, never a string

```
INSTALL_DEPENDENCIES   →  npm install --no-audit --no-fund
TYPECHECK              →  npx --no-install tsc --noEmit
LINT                   →  npx --no-install eslint .
BUILD                  →  npm run build --if-present
START_DEV_SERVER       →  npm run dev -- --port 5173 --strictPort --host 127.0.0.1
```

`SandboxCommand(operation, framework).resolve()` is the only place an
operation becomes an argv, from a fixed table. Any other framework raises
`UNSUPPORTED_OPERATION`.

## Image

`node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293`
— digest-pinned; used for sandboxes **and** the egress proxy (no third-party
proxy image). No configuration, API field or project setting selects it.

## Container configuration (every sandbox, unconditionally)

| Control | Flag | Status / evidence |
|---|---|---|
| No privilege escalation | `--security-opt no-new-privileges` | **VERIFIED STATE**: `NoNewPrivs: 1` on the sandboxed process. |
| Seccomp | Docker default profile | **VERIFIED STATE**: `Seccomp: 2`. |
| No Linux capabilities | `--cap-drop ALL` | **ENFORCED BY DOCKER**: all `Cap*` sets zero; `mknod` fails. |
| Non-root | `--user 1000:1000` | **ENFORCED BY DOCKER**: uid 1000, also inside lifecycle scripts. |
| PID limit | `--pids-limit` | **ENFORCED BY DOCKER**: fork bomb stopped. |
| Memory | `--memory` = `--memory-swap` | **ENFORCED BY DOCKER**: OOM-kill observed; no swap headroom. |
| CPU | `--cpus` | **ENFORCED BY DOCKER**: measured 0.244 / 0.498 / 0.988 cores at limits 0.25 / 0.5 / 1.0. |
| Read-only root | `--read-only` | **ENFORCED BY DOCKER**. |
| Scratch | `--tmpfs /tmp:rw,size=<storage_mb>m` | **ENFORCED BY DOCKER** (`ENOSPC`). |
| Project mount | one bind mount, `workspace.project_root` | **ENFORCED BY DOCKER**; size **PERIODICALLY ENFORCED**. |
| Ownership | `redstone.managed=true` + opaque ids | forged/escaping labels rejected. |
| Never constructed | `--privileged`, `--network host`/`bridge`, `--pid`, `--ipc`, `--userns host`, `--uts host`, `--device`, `--cap-add`, `-p`/`--publish`, `docker.sock` | statically asserted for both policies, sandbox **and** proxy. |

## Network (Phase 4.2)

### Topology

```
RUN PHASE + VALIDATION  (NetworkPolicy.DENY)
    sandbox            --network none          no network device at all

INSTALL PHASE  (NetworkPolicy.INSTALL_ONLY)
    sandbox ──── <id>-net ──── <id>-proxy ──── <id>-egress ──── internet
              Docker --internal              ICC off, only member
              members: exactly the sandbox   is the proxy
              and its own proxy
```

One proxy and two networks **per install sandbox**, all created in `create()`
and removed by `destroy()` (names derived from the sandbox id, so cleanup works
after a restart). No shared infrastructure, so there is no cross-workspace
policy, no shared credentials and nothing one workspace can destroy for
another.

`<id>-net` is a Docker `--internal` network: no gateway, no NAT. Verified on
this daemon:

- A direct connection to any address outside it fails with `ENETUNREACH` — no
  route, not a filter that could be raced.
- Docker's embedded DNS doesn't resolve external names on it (`SERVFAIL` /
  `EAI_AGAIN`), so DNS is neither a route nor an exfiltration channel.

**The sandbox's entire reachable world is therefore its proxy's policy.**

### What is enforced

| Destination from an install sandbox | Outcome | How |
|---|---|---|
| `registry.npmjs.org:443` via the proxy | **allowed** | **ENFORCED BY PROXY**: exact host, port 443, public address only |
| arbitrary internet (`example.com`, `google.com`, `github.com`, `raw.githubusercontent.com`, other registries) | **blocked** | **BY DOCKER** (no route) **and BY PROXY** (CONNECT → 403) |
| arbitrary public IP (`1.1.1.1:443`, `8.8.8.8:53`) | **blocked** | both |
| the allowed registry's **own IP** | **blocked** | both — IP literals are never allowed, so resolving the name yourself doesn't help |
| RFC1918 (`10.0.0.1`, `172.16.0.1`, `192.168.1.1`) | **blocked** | both |
| loopback (`127.0.0.1`, `localhost`, `[::1]`) | **blocked** | proxy refuses; `127.0.0.1` inside the sandbox is its own empty loopback |
| link-local / cloud metadata (`169.254.1.1`, `169.254.169.254`) | **blocked** | both (**ENVIRONMENT LIMITATION**: no real metadata service here, so blocked by construction and verified by address) |
| Docker gateway `172.17.0.1` | **blocked** | both |
| other Redstone sandboxes, other workspaces' proxies, unrelated containers | **blocked** | **BY DOCKER** (separate internal networks) and **BY PROXY** (IP literal) |
| the proxy on any port but 3128 | **blocked** | nothing else listens |
| allowed host on another port (`registry.npmjs.org:80`) | **blocked** | **BY PROXY** |
| name tricks: `registry.npmjs.org.evil.example`, `www.registry.npmjs.org`, `registry.npmjs.org@evil.example`, `0x7f.1` | **blocked** | **BY PROXY** (exact match; malformed/numeric names refused) |
| plain HTTP / absolute-URI forwarding (`GET http://…`) | **refused (405)** | **BY PROXY** |
| inbound from anywhere | none | nothing published |

Each row is a real-Docker test in `tests/redstone/test_install_egress.py`,
run from inside a real `INSTALL_ONLY` sandbox.

### Proxy policy (`src/redstone/sandbox/egress_proxy.js`)

- **`CONNECT` only.** No plain-HTTP forwarding, no absolute-URI requests. The
  proxy never makes a request itself, never sees inside TLS, and **never
  follows a redirect**. A redirect to another host costs the client a new
  `CONNECT`, which is judged like any other — a redirect cannot escape the
  allowlist.
- **Exact allowlist match** (case-insensitive, one trailing dot tolerated).
  IP literals, IPv6 brackets, userinfo, wildcards, and names whose last label
  is numeric are refused before any DNS lookup.
- **Every resolved address must be public unicast.** One loopback, RFC1918,
  CGNAT, link-local, reserved, multicast, ULA, v4-mapped-private, 6to4 or
  documentation answer denies the whole request.
- **The upstream connection goes to the exact address that was validated** —
  the name is never resolved a second time. See "DNS" below.
- **Bounded:**

  | Limit | Value |
  |---|---|
  | request header | 8 KB, 10 s to arrive |
  | concurrent tunnels | 32 (configurable) |
  | upstream connect | 10 s (configurable) |
  | idle tunnel | 60 s |
  | tunnel lifetime | 600 s |
  | bytes per tunnel | 512 MB |
  | proxy startup | 20 s, then the install fails |

- **Logs decisions only** — host, port, decision, reason, validated address.
  Never headers; a `Proxy-Authorization` canary never appears in its logs
  (tested).
- **Refuses to start** with an empty, numeric, IP or wildcard allowlist
  (tested). It never runs with an unintended policy.
- **Hardened like a sandbox:**
  - non-root, `--cap-drop ALL`, no-new-privileges, read-only root;
  - 128 MB memory, 64 PIDs, 0.5 CPU, no published ports;
  - its only mount is the script, read-only;
  - labelled `redstone.role=egress-proxy` and with the owning runtime's ids,
    so orphan recovery finds it.

### Why no environment or config change can bypass it

The proxy settings Redstone injects (`HTTP(S)_PROXY`, lowercase variants,
`npm_config_(https_)proxy`, `NO_PROXY=`, `npm_config_registry`) only help
legitimate npm traffic *find* the proxy. Enforcement doesn't depend on them.
Tested from inside a real install sandbox, and from inside a hostile
lifecycle script:

| Attempt | Result |
|---|---|
| unset every proxy variable | npm fails — no route |
| `NO_PROXY=*` | nothing new reachable (npm keeps honouring its explicit https-proxy config, so even its allowed traffic still passes the policy — seen in the proxy log) |
| `HTTPS_PROXY=http://1.1.1.1:8080` (attacker proxy) | unreachable |
| `--registry=https://registry.yarnpkg.com/` or `https://example.com/` | proxy 403 |
| a project `.npmrc` with `registry=…` | proxy 403 (logged as a denial) |
| direct TCP to the registry's resolved IP | no route |

### DNS

1. **Sandbox:** can't resolve external names at all.
2. **Proxy:** resolves the allowlisted name (`dns.lookup`, all answers) and
   validates every answer. Real-proxy tests pin answers with `--add-host`, so
   they need no public DNS service: public → 200; private, CGNAT, loopback,
   link-local, metadata, `::1`, `fd00::1`, `::ffff:10.0.0.5` → 403; a name with
   one public and one private answer → 403.
3. **Rebinding:** the proxy connects to the IP it validated, so a second
   lookup can't swap in a private address between check and connect.
4. **Residual — NOT ENFORCED:** the proxy trusts the resolver's *public*
   answers. A poisoned resolver could point `registry.npmjs.org` at an
   attacker's public IP. npm still verifies the registry's TLS certificate end
   to end (the proxy is a blind tunnel), so impersonation would also need a
   valid certificate for the registry's name.

### Fail closed

There is no fallback network anywhere in the code: `INSTALL_ONLY` has exactly
one implementation.

- **The proxy can't be built** — script missing, crashes at startup, never
  ready, or a bad policy: `create()` raises `CREATE_FAILED` before the sandbox
  exists, and leaves no container or network behind (tested for all three
  failure modes).
- **The proxy dies mid-install** (tested with `docker stop`): npm fails, there
  is no route to anything, and the sandbox is still on `<id>-net` only.
- **`REDSTONE_INSTALL_NETWORK_ENABLED=false`** runs installs with `DENY` — no
  network at all, never more.

### Proxy compromise model

If a lifecycle script found a bug in the proxy's ~300 lines (no npm
dependencies, Node core `net`/`dns` only) and took over the proxy process, it
would gain the proxy's position:

- **it could reach:** the internet from `<id>-egress`;
- **it could not reach:** other containers (ICC off, sole member), the
  Docker socket, host mounts, or capabilities;
- **it would still be:** non-root, read-only, 128 MB / 64 PIDs, and destroyed
  with the install.

The proxy is trusted code whose correctness this boundary relies on, and that
is stated here rather than implied away.

## Preview network (Phase 6)

`NetworkPolicy.PREVIEW`, used only for `START_PREVIEW_SERVER` (the dev
server bound to `0.0.0.0` inside its own network namespace):

```
app ── <id>-net (--internal) ── <id>-relay ── <id>-pub (ICC off) ── 127.0.0.1:<random port>
```

- **The app:** no route out, nothing published, no external DNS. Its only
  peer is its own relay.
- **The relay** (`preview_relay.js`):
  - demands a per-preview 256-bit token — required because other containers
    can reach loopback-published ports via `host.docker.internal`, which was
    verified;
  - forwards only to `<sandbox>:5173`;
  - refuses `CONNECT` and upgrades;
  - is hardened like the egress proxy.
- `destroy()` removes the relay and both networks by derived name.
  `LocalProcessSandboxProvider.preview_upstream()` always returns `None`, so
  previews are refused there.

Full model — gateway, browser origins, lifecycle — in `docs/redstone/PREVIEW.md`.

## Storage

**PERIODICALLY ENFORCED** — the Phase 4.1 watchdog, unchanged. A bind mount
can't be capped by any Docker flag, so Redstone measures real on-disk usage of
the mount every 1.0 s by default and kills the sandbox past `storage_mb`.

- Overshoot is roughly throughput × interval (17–18 MB measured at 0.2 s).
- It depends on the Redstone process being alive.
- `/tmp` is kernel-bounded.

## Output

Separate stdout/stderr, an exact `truncated` flag, and the sandbox's own
`output_bytes` limit (Phase 4.1).

## Filesystem

The one writable mount is `workspace.project_root`, hardened by Phase 2A/2A.1.

- **Phase 2A.1** protects Redstone's own host-side file operations.
- **Phase 4** confines code inside the container: symlinks it creates resolve
  inside the container's mount namespace; hardlinks to `/etc/passwd`,
  `/etc/shadow` and `node` fail; host canaries are never readable (real-Docker
  tests).

**Windows hosts:**

- Links a sandbox writes onto the bind mount (npm's `node_modules/.bin/*`)
  can't be opened by the host.
- `read_file` on them fails with a generic error, with **no host path
  disclosed**.
- `WorkspaceManager.destroy()` removes them — fixed in 4.1 and tested.

## Devices

- Only pseudo-devices are present; there are no block devices.
- `mknod` fails.
- `--device` and `--privileged` are never constructed.
- `test_device_posture` verifies all of this against a real container.

## no-new-privileges

- **Flag:** verified.
- **Kernel state:** verified (`NoNewPrivs: 1`).
- **Escalation attempt:** not performed — it would need a purpose-built
  setuid test image.

## npm lifecycle scripts

Lifecycle scripts run (no `--ignore-scripts`). `test_hostile_postinstall_script_is_contained`
installs a local, never-published tarball whose `postinstall` tries everything
below. It runs with real canary secrets planted in the trusted process's own
environment:

| Probe | Result |
|---|---|
| secret-looking env vars; planted `GEMINI_API_KEY` / `REDSTONE_DEPLOY_SECRET` | absent everywhere |
| read `/etc/shadow`, a host file beside the mount | denied / not found |
| write `/etc`, `/usr/local/lib`, `/workspace` | denied (read-only root); `/tmp` allowed and bounded |
| another container, `127.0.0.1`, `10.0.0.1`, `169.254.169.254` | blocked |
| arbitrary internet directly (`1.1.1.1`) | **blocked (new in 4.2)** |
| the registry's own IP directly | **blocked** |
| proxy `CONNECT` to `example.com`, the metadata address, the registry's IP | **403** |
| npm with `NO_PROXY=*` + a foreign registry; with proxy vars removed; with an attacker proxy; with `--registry=https://example.com/` | **all fail** |
| 400 processes | stopped by `--pids-limit` |

## Two providers

| | `DockerSandboxProvider` | `LocalProcessSandboxProvider` |
|---|---|---|
| `is_isolated` | `True` | `False` |
| Network | `DENY` / proxied `INSTALL_ONLY` / relayed `PREVIEW` | **none of this** — the host's own network |
| Live preview | supported (relay topology) | **refused** — `preview_upstream()` is always `None` |
| `storage_mb` | periodically enforced | not enforced |
| `list_managed()` | Docker labels | always empty |

## Testing

| File | Tests | Kind |
|---|---|---|
| `test_sandbox.py` | 35 | 17 real Docker, 1 static, 8 local-process, 9 unit |
| `test_sandbox_hardening.py` | 30 | 28 real Docker, 2 static |
| `test_install_egress.py` | 62 | **all real Docker** (Phase 4.2) |
| `test_install_egress_policy.py` | 52 | unit (policy validation, config parsing, RuntimeManager network choice) |
| `test_preview*.py` | 149 | Phase 6 — see `PREVIEW.md` (real Docker, real Chrome, unit, stand-in relay) |
| `test_sandbox_validation.py` | 8 | 2 real Docker, 6 fake |

Real-Docker tests skip with an explicit reason when no daemon is reachable;
they're never mocked to fake a pass.

Measured real `npm install` (with a transitive dependency), through the
proxy:
- proxy + sandbox creation: 1.41 s;
- install: 2.19 s;
- cleanup: 1.39 s;
- 2 tunnels, both to `registry.npmjs.org`, and 0 denials — npm needs no other
  host.

## Known limitations

- **DNS answers for allowlisted names are trusted if public** — see DNS; TLS
  is the remaining safeguard.
- **The allowed registry is itself a destination.** Data a script sends there
  reaches npm, not the attacker, except through side channels such as download
  statistics. Low bandwidth, not mitigated. The proxy restricts *where*, not
  *what*: malicious packages *on* the registry still install — that's npm
  supply chain, out of scope for the network boundary.
- **The proxy is trusted code** (see Proxy compromise model).
- **Each operator-added registry host is additional reachable surface** for
  arbitrary lifecycle code.
- **IPv6 is not exercised end to end** — these networks are IPv4-only. The
  classifier's IPv6 handling is tested with pinned answers.
- **Storage is periodically enforced**, and no setuid escalation attempt was
  made.
- **Cloud metadata can't be reproduced here** — it's blocked by construction
  at both layers.
- **`LocalProcessSandboxProvider` isolates nothing**, network included.
