# Redstone sandbox

Phase 4, hardened in Phase 4.1. A provider-neutral abstraction
(`redstone.sandbox`) for executing one untrusted, bounded or long-running
process — a generated project's dependency install, its dev server, or a
typecheck/lint/build — behind a real security boundary.
`redstone.runtime.RuntimeManager` (Phase 5, see `RUNTIME.md`) and
`SandboxValidationRunner` are the only things above this layer that call it.

Every claim below is labelled with one of:

| Label | Meaning |
|---|---|
| **ENFORCED** | A kernel/Docker mechanism blocks it, and a real-Docker test drove the mechanism to its limit and observed the block. |
| **POLL-ENFORCED** | Redstone actively measures and kills, and a real-Docker test proved it — but the block is not instantaneous. |
| **VERIFIED STATE** | The kernel reports the protective state on the real sandboxed process; no attack was attempted against it. |
| **NOT ENFORCED** | Known, open gap. Documented here and in `SECURITY.md`; characterization-tested so it can't silently change. |
| **ENVIRONMENT LIMITATION** | Can't be reproduced on this development machine. |

> **Environment.** Built and tested against a real Docker daemon (Docker
> Desktop 29.4.1, WSL2 backend — a genuine Linux kernel) — not assumed.
> `docker_available()` is checked at startup, never hardcoded. With no
> daemon, `best_available_provider()` falls back to
> `LocalProcessSandboxProvider`, which declares `is_isolated = False` and
> provides no isolation at all; `/api/health` reports which one is active.

## Architecture

```
SandboxProvider (Protocol, redstone/sandbox/provider.py)
   create(config) -> sandbox_id      prepare, do not start
   start(sandbox_id)                 run; Docker also arms the storage watchdog
   wait(sandbox_id, timeout)         BOUNDED op: block for exit; kill the whole
                                      process tree on timeout
   exec_in(sandbox_id, argv, timeout) one probe inside a running sandbox
   status(sandbox_id) -> SandboxStatus   incl. resource_limit_exceeded
   logs(sandbox_id, max_bytes) -> str    merged, bounded, for humans
   stop / kill / destroy(sandbox_id)     all idempotent
   list_managed() -> entries             Redstone-owned sandboxes, read from the
                                         backend itself (Phase 4.1)
```

Two execution shapes: **bounded** (`INSTALL_DEPENDENCIES`, `TYPECHECK`,
`LINT`, `BUILD`: `create → start → wait → destroy`) and **long-running**
(`START_DEV_SERVER`: `create → start → poll health → stop/kill → destroy`).

## Command policy: named operations, never a string

```
Operation                 Framework.REACT_VITE_TS command (fixed table)
INSTALL_DEPENDENCIES   →  npm install --no-audit --no-fund
TYPECHECK              →  npx --no-install tsc --noEmit
LINT                   →  npx --no-install eslint .
BUILD                  →  npm run build --if-present
START_DEV_SERVER       →  npm run dev -- --port 5173 --strictPort --host 127.0.0.1
```

`SandboxCommand(operation, framework).resolve()` is the only place an
operation becomes an argv, from a fixed table written by Redstone. No agent
output, project content or API field reaches an executable or argument. Any
other framework raises `UNSUPPORTED_OPERATION`.

## Image

`node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293`
— **digest-pinned** in Phase 4.1. The digest is the manifest-list digest
`node:20-alpine` resolved to on this daemon (`docker image inspect` and
`docker buildx imagetools inspect` agree). A tag can be repointed upstream; a
digest cannot. The image is a constant in `docker_provider.py`: there is no
config field, environment variable, API field or project setting that selects
it (`test_no_environment_variable_selects_the_runtime_image`). Rotating it is
a reviewed code change.

## Container configuration (every container, unconditionally)

| Control | Flag | Status / evidence |
|---|---|---|
| No privilege escalation | `--security-opt no-new-privileges` | **VERIFIED STATE**: `/proc/self/status` of the sandboxed process shows `NoNewPrivs: 1` (`test_no_new_privs_bit_is_set_on_the_sandboxed_process`). See "no-new-privileges" below for what was *not* tested. |
| Seccomp | Docker default profile | **VERIFIED STATE**: `Seccomp: 2` (filter mode) on the same process. |
| No Linux capabilities | `--cap-drop ALL` | **ENFORCED**: all five `Cap*` sets read `0000000000000000`; `mknod` fails. |
| Non-root | `--user 1000:1000` | **ENFORCED**: `id -u` → `1000`; a hostile lifecycle script also reports uid 1000. |
| PID limit | `--pids-limit` | **ENFORCED**: fork bomb fails at the limit; also held inside an npm lifecycle script. |
| Memory | `--memory` + `--memory-swap` (equal) | **ENFORCED**: 200 MB allocation vs 64 MB limit → OOM-kill. `--memory-swap` equal to `--memory` (new in 4.1) removes swap headroom beyond the limit. |
| CPU | `--cpus` | **ENFORCED** (new evidence in 4.1): kernel-accounted CPU time of a single-threaded busy loop measured at 0.244 / 0.498 / 0.988 cores for limits 0.25 / 0.5 / 1.0 (and 1.002 at 2.0, the one-thread ceiling). `test_cpu_limit_is_behaviourally_enforced`. |
| Read-only root | `--read-only` | **ENFORCED**: writes to `/etc`, `/usr/local`, `/workspace` fail (`EROFS`/`EACCES`), including from a hostile lifecycle script. |
| Scratch space | `--tmpfs /tmp:rw,size=<storage_mb>m,mode=1777` | **ENFORCED** (new in 4.1): previously unbounded (Docker's default lets tmpfs reach half of host RAM); now `dd` past the size fails with `No space left on device`. |
| Project mount | one bind mount, `workspace.project_root` only | **ENFORCED** by construction — see Filesystem. Size: **POLL-ENFORCED**, see Storage. |
| Ownership label | `--label redstone.managed=true` + `redstone.{project,workspace,runtime}_id`, `redstone.purpose` | New in 4.1. Opaque ids only, never secrets; keys outside `redstone.*` and attempts to override `redstone.managed` are rejected (`test_labels_cannot_forge_or_escape_the_ownership_namespace`). |
| Bounded logs | `--log-driver json-file --log-opt max-size --log-opt max-file=1` | Docker-side bound on retained logs. |
| Never constructed | `--privileged`, `--network host`, `--pid`, `--ipc`, `--userns host`, `--uts host`, `--device`, `--cap-add`, `-p`/`--publish`, `docker.sock` mount | Statically asserted against the real constructed argv for both network policies (`test_constructed_argv_has_every_required_flag_and_no_forbidden_one`). |

## Storage (Phase 4.1)

`ResourceLimits.storage_mb` (default 1024). **Why not a Docker flag:** a
bind-mounted directory cannot be size-capped by any container option. Docker's
`--storage-opt size=` bounds only a container's own copy-on-write layer — never
a bind mount — and needs specific storage drivers besides. A kernel-level
quota on the mount would need a loop-mounted fixed-size filesystem or XFS
project quotas on the host, both requiring privileged setup this phase forbids.

**What Redstone does instead — POLL-ENFORCED.** When a sandbox starts,
`DockerSandboxProvider` arms a watchdog thread that measures real on-disk usage
of the sandbox's writable mounts from the host side (`os.walk`, never
following links, so a planted link can't make it measure or walk anything
outside the mount) every `storage_poll_interval` seconds (default 1.0). Once
usage exceeds the ceiling it kills the sandbox — the same whole-process-tree
kill as a timeout — and records `resource_limit_exceeded = "storage_mb"` on
`SandboxStatus` and `SandboxResult`. `RuntimeManager` turns that into
`RUNTIME_RESOURCE_LIMIT`.

What is measured is the untrusted process's own direct writes (`dd` inside the
container, `npm install`, a lifecycle script) — not anything routed through
Redstone's Python file API.

| Evidence (real Docker, 0.2 s poll) | Limit | Killed after | On disk at kill | Overshoot |
|---|---|---|---|---|
| continuous 2 MB `dd` writes | 8 MB | 0.6 s | 25 MB | 17 MB |
| same | 64 MB | 1.2 s | 82 MB | 18 MB |

**Limits of this mechanism, stated plainly:**

- **Not instantaneous.** A writer can exceed the ceiling by roughly *write
  throughput × poll interval* before it is killed. This bind mount sustained
  ~85 MB/s, so at the production default of 1.0 s the overshoot could be
  ~100 MB. The host disk is protected against *unbounded* growth, not against
  a bounded burst above the ceiling.
- **Cost.** Each poll walks the whole project tree, including `node_modules`.
- **Liveness.** It depends on the Redstone process being alive. If Redstone
  dies while a sandbox is writing, nothing measures it until restart
  reconciliation destroys that sandbox (`RUNTIME.md`, Orphan recovery).
- **Files survive.** Killing the writer doesn't delete what it wrote; the
  bytes stay in the workspace until the project's files are cleaned up.

Every storage channel a sandbox has: project mount (**POLL-ENFORCED**),
`/tmp` tmpfs (**ENFORCED**, kernel `ENOSPC`), container root (**ENFORCED**
read-only, so no copy-on-write layer growth), logs (Docker `max-size`,
`max-file=1`).

## Output (Phase 4.1 fixes)

- **Streams are genuinely separate.** `wait()` and `exec_in()` read stdout and
  stderr through two independent pipes (`docker logs` reproduces each
  container stream on the matching CLI stream — verified against this daemon).
  Both pipes are drained concurrently to EOF so the CLI can't deadlock, while
  each side stops *retaining* text past the ceiling: Redstone holds at most
  2 × `output_bytes`. Previously `SandboxResult.stderr` was always `""`.
- **`truncated` is exact.** `True` if and only if either stream produced more
  than `output_bytes`. Previously the Docker `wait()` path hardcoded `False`.
- **The ceiling is the sandbox's own `output_bytes`**, remembered per sandbox —
  previously `wait()` ignored it and used a hardcoded 256 KB.
- **Head kept.** Truncated output keeps the beginning — for install/build
  failures the first error is the useful one.
- `logs()` stays a single merged, interleaved string on purpose — it's for
  human diagnosis. Structured callers use `SandboxResult`.

Tests: below the limit, exactly at it (4096 bytes → not truncated), over it,
a 2 MB stdout flood, a 2 MB stderr flood, and stream separation.

## Network

| Destination | `DENY` (run phase, validation) | `INSTALL_ONLY` (npm install only) |
|---|---|---|
| npm registry | blocked — **ENFORCED** | reachable (intended) |
| arbitrary internet host | blocked — **ENFORCED** | **reachable — NOT ENFORCED** (`test_install_network_egress_is_not_registry_restricted`) |
| another container on Docker's default bridge (Redstone's or the user's, e.g. a local database) | blocked — **ENFORCED** | blocked — **ENFORCED, new in 4.1** (`test_install_network_cannot_reach_other_containers`, with a positive control proving the target was reachable from the default bridge) |
| Docker default-bridge gateway `172.17.0.1` | blocked — **ENFORCED** | not on that bridge; its own network's gateway (the host side) is routable — **NOT ENFORCED** |
| RFC1918 (`10.0.0.1`, `192.168.1.1`) | blocked — **ENFORCED** | depends on host routing — **NOT ENFORCED** |
| `169.254.169.254` metadata | blocked — **ENFORCED** | nothing answered here — **ENVIRONMENT LIMITATION** (not a cloud host; a real metadata endpoint would *not* be blocked) |
| `127.0.0.1` | the sandbox's own empty loopback (connection refused) | same |
| inbound from anywhere | nothing published — **ENFORCED** | nothing published — **ENFORCED** |

**`DENY`** is `--network none`: no network device at all. Every
non-loopback target fails with *network unreachable* — no route, not a
filter that could be raced (`test_deny_policy_has_no_route_to_anything`,
five destinations).

**`INSTALL_ONLY`** changed in 4.1 from Docker's shared default `bridge` to a
**dedicated, per-sandbox bridge network** (`<sandbox_id>-net`, ICC disabled,
labelled `redstone.managed=true`), created with the sandbox and removed by
`destroy()`. The name is derived from the sandbox id, so destroy can remove it
even after a Redstone restart. Before 4.1, an install container on the default
bridge **could** reach any other container there — confirmed empirically
during this phase (`CONNECTED` to an unrelated container); that exposure is
now closed.

**What 4.1 did not fix:** registry-only egress. Stock Docker has no way to
restrict a bridge network's outbound traffic to a hostname allowlist without
either an egress proxy (a new architectural component, out of scope for a
hardening-only phase) or host firewall rules (which need privileged/`NET_ADMIN`
access this project forbids). Filtering by hostname in Python would not
constrain the container at all and was deliberately not done.
`NetworkPolicy.ALLOWLIST` stays declared and unimplemented (it raises if
selected). **Until a proxy exists, an npm lifecycle script can reach arbitrary
internet hosts during install.** It can't reach Redstone's environment or
files (they aren't network-reachable), other containers, or anything after
install ends.

## Filesystem

The one writable mount is `Workspace.project_root`, already hardened by Phase
2A/2A.1. `redstone.sandbox`/`redstone.runtime` add no path-resolution logic.

**Two separate trust boundaries — not to be confused:**

- **Phase 2A.1 (host side)** protects Redstone's *own* file operations
  (`read_file`/`write_file`/snapshots) from traversal, symlink, junction,
  hardlink, ADS and alias tricks.
- **Phase 4 (container side)** confines code running *inside* the sandbox.
  Every path it names, including through symlinks it creates, resolves inside
  the container's own mount namespace, where the host's files don't exist.

Container-side evidence (real Docker, new in 4.1):

- A canary file placed on the host **beside** the mount is unreachable through
  `ln -s ..` (in `/tmp` and in the mount), `ln -s /`, or a symlink to its exact
  host path; the canary never appears in output
  (`test_symlinks_inside_the_sandbox_cannot_reach_the_host`).
- Hardlinking `/etc/passwd`, `/etc/shadow` or `/usr/local/bin/node` into the
  mount fails, and nothing appears on the host
  (`test_hardlinks_inside_the_sandbox_cannot_capture_files_outside_the_mount`).
- A sibling "other project" directory with a secret is unreadable
  (`test_filesystem_cannot_reach_outside_the_mount`, Phase 4).

A symlink the sandbox leaves *inside* the project is later subject to the
Phase 2A.1 checks whenever Redstone's own code reads it — the two layers
compose; neither substitutes for the other.

**Windows-host finding (Phase 4.1).** On Docker Desktop, a Linux symlink a
sandbox writes onto the Windows bind mount — `npm install` creates them
routinely (`node_modules/.bin/*`) — appears on the host as a reparse point
the host can't open (`WinError 1920`). Verified consequences:

- `list_files` skips such entries (the 2A.1 walk never descends reparse points).
- `read_file`/`delete_file` on them fail with a raw `OSError`, which the agent
  tool layer converts to a generic `INTERNAL_ERROR` carrying only the
  exception type — **no host path reaches the model or any API response**.
  Safe, but the error isn't a friendly one.
- **`WorkspaceManager.destroy()` used to fail on them**, leaving real npm
  projects undeletable. **Fixed in 4.1:** its `rmtree` error handler removes
  the link entry itself (only the link, never the target), and
  `test_workspace_with_sandbox_created_links_can_still_be_destroyed` proves it
  against real Docker.

## Devices (Phase 4.1)

Docker's defaults provide the device protection; 4.1 states and verifies them
explicitly instead of assuming:

- **Visible devices:** only pseudo-devices — `/dev/{null,zero,full,random,urandom,tty,console,ptmx}`,
  `fd`, `stdin/stdout/stderr`, `pts`, `shm`, `mqueue`, `core`. No block devices
  (`find /dev -type b` is empty); no `/dev/mem`, `/dev/kmem`, `/dev/kmsg`,
  `/dev/sda`, `/dev/nvme0n1`.
- **Device creation:** impossible — `CAP_MKNOD` is dropped; `mknod` fails.
- **`--device`:** never constructed, for any config (statically asserted).
- **Privileged mode:** never constructed (statically asserted), so Docker
  never exposes host devices.

`test_device_posture` asserts all of the above against a real container.

## no-new-privileges — what exactly is verified

- **FLAG VERIFIED:** present in every constructed argv (static test).
- **KERNEL STATE VERIFIED:** `NoNewPrivs: 1` on the real sandboxed process —
  the kernel will not raise its privileges across `execve()` of a
  setuid/setgid/file-capability binary.
- **ESCALATION ATTEMPT: NOT PERFORMED.** A true behavioural test needs a
  root-owned setuid binary baked into a purpose-built test image, run once with
  and once without the flag. That's a new build artifact and supply chain,
  deliberately not added in a hardening-only phase. Non-root + `--cap-drop ALL`
  (both proven) already deny the usual escalation paths independently.

## npm lifecycle scripts

`npm install` runs lifecycle scripts (no `--ignore-scripts` — real packages
depend on them), inside the ephemeral install sandbox, destroyed right after.
`test_hostile_postinstall_script_is_contained` installs a **local,
never-published** tarball (`tests/redstone/fixtures/malicious_lifecycle/`)
whose `postinstall` probes, through the real `INSTALL_DEPENDENCIES` operation,
with real secrets planted in the trusted process's own environment:

| Probe | Outcome |
|---|---|
| identity | uid 1000 |
| secret-looking env vars (`*KEY*`, `*TOKEN*`, `*SECRET*`, `REDSTONE_*`, `AI_*`, …); planted `GEMINI_API_KEY` / `REDSTONE_DEPLOY_SECRET` canaries | none present; canary absent from every output |
| read `/etc/shadow`, a host file beside the mount (by host path and by `..`) | denied / not found |
| write `/etc`, `/usr/local/lib`, `/workspace` | denied (read-only root) |
| write `/tmp` | allowed — bounded tmpfs |
| connect to an unrelated container on the default bridge | blocked |
| connect to `169.254.169.254` | no answer (**ENVIRONMENT LIMITATION**) |
| spawn 400 processes | stopped by `--pids-limit` (< 128 alive) |
| arbitrary internet | **reachable — NOT ENFORCED** (see Network) |

## Two providers

| | `DockerSandboxProvider` | `LocalProcessSandboxProvider` |
|---|---|---|
| `is_isolated` | `True` | `False` |
| Isolation | real namespaces / cgroups / capabilities | none — same OS user, filesystem, network as Redstone |
| `storage_mb` | POLL-ENFORCED | **not enforced** |
| `list_managed()` | reads Docker labels | always empty (its children die with Redstone) |
| Separate stdout/stderr | yes | no (merged, `stderr=""`) |
| Process-tree kill | whole PID namespace | POSIX process group; best-effort on Windows |
| Selected when | Docker reachable | only when Docker isn't — visible at `/api/health` |

## Testing

| File | Tests | Kind |
|---|---|---|
| `tests/redstone/test_sandbox.py` | 35 | Phase 4: 17 real Docker, 1 static, 8 local-process, 9 unit |
| `tests/redstone/test_sandbox_hardening.py` | 30 | Phase 4.1: 28 real Docker, 2 static argv (monkeypatched `subprocess.run`, no container) |
| `tests/redstone/test_sandbox_validation.py` | 8 | 2 real Docker, 6 fake provider |

Docker tests skip with an explicit reason when no daemon is reachable; they're
never mocked to fake a pass. Tests named `test_*_is_not_*` are
characterization tests of documented open gaps.

## Known limitations

- **Registry-only install egress is NOT ENFORCED** — needs an egress proxy.
- **Storage is poll-enforced**, with overshoot ≈ throughput × interval, and
  depends on the Redstone process being alive.
- **No setuid escalation attempt** was performed for no-new-privileges (kernel
  state verified instead).
- **Cloud metadata** was not reproducible here.
- **`LocalProcessSandboxProvider` isolates nothing**, including storage.
- **Image digest** is correct as of this phase; rotating it (for security
  updates) is a manual, reviewed change.
