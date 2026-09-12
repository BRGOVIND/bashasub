# Redstone sandbox

Phase 4. A provider-neutral abstraction (`redstone.sandbox`) for executing one
untrusted, bounded or long-running process — a generated project's dependency
install, its dev server, or a typecheck/lint/build — behind a real security
boundary. `redstone.runtime.RuntimeManager` (Phase 5, see `RUNTIME.md`) is the
only thing above this layer that calls it directly.

> **Environment honesty.** This was built and tested against a real Docker
> daemon (Docker Desktop 29.4.1, WSL2 backend) reachable in this development
> environment — not assumed. `docker_available()` is checked at startup, never
> hardcoded to true. When no daemon is reachable, `best_available_provider()`
> falls back to `LocalProcessSandboxProvider`, which declares
> `is_isolated = False` and is documented, not disguised, as providing no real
> isolation — see "Two providers" below.

## Architecture

```
SandboxProvider (Protocol, redstone/sandbox/provider.py)
   create(config) -> sandbox_id      prepare, do not start
   start(sandbox_id)                 begin running the configured command
   wait(sandbox_id, timeout)         BOUNDED op: block for exit, kill the
                                      whole process tree on timeout
   exec_in(sandbox_id, argv, timeout) one extra probe inside a running
                                      sandbox (a health check), works even
                                      under NetworkPolicy.DENY
   status(sandbox_id) -> SandboxStatus
   logs(sandbox_id, max_bytes) -> str   always truncated, never unbounded
   stop / kill / destroy(sandbox_id)     all idempotent

        ┌─────────────────────┐        ┌──────────────────────────┐
        │ DockerSandboxProvider│        │ LocalProcessSandboxProvider│
        │ is_isolated = True   │        │ is_isolated = False        │
        │ real namespaces,     │        │ subprocess.Popen, same OS  │
        │ cgroups, capabilities│        │ user/fs/net as Redstone    │
        └─────────────────────┘        └──────────────────────────┘
```

Two execution shapes, matching what actually needs each:

* **BOUNDED** (`INSTALL_DEPENDENCIES`, `TYPECHECK`, `LINT`, `BUILD`):
  `create → start → wait` — the caller blocks until exit or timeout.
* **LONG-RUNNING** (`START_DEV_SERVER`): `create → start → (caller polls
  `status()`/`exec_in()` for health) → stop/kill → destroy`. `wait()` is never
  called for this shape; `RuntimeManager` owns the lifecycle instead.

## The command policy: named operations, never a string

```
Operation                 Framework.REACT_VITE_TS command (fixed table)
─────────────────────     ──────────────────────────────────────────────
INSTALL_DEPENDENCIES   →  npm install --no-audit --no-fund
TYPECHECK              →  npx --no-install tsc --noEmit
LINT                   →  npx --no-install eslint .
BUILD                  →  npm run build --if-present
START_DEV_SERVER       →  npm run dev -- --port 5173 --strictPort --host 127.0.0.1
```

`SandboxCommand(operation, framework).resolve()` is the **only** place an
`Operation` becomes an actual argv, from a fixed `dict[Framework,
dict[Operation, tuple[str, ...]]]` written entirely by Redstone. There is no
code path from agent output, project content, or an API request to an
arbitrary executable or argument — not `run_command(command)`, not a
`script_name` parameter, nothing. Only `Framework.REACT_VITE_TS` has table
entries; any other framework (including `Framework.STATIC`) raises
`UNSUPPORTED_OPERATION` rather than guessing a command. This is the same "no
arbitrary command surface" discipline the Phase 3 tool registry already
applies, extended one layer down.

## Security posture (every container, no exceptions)

Applied by `DockerSandboxProvider.create()` regardless of caller-supplied
config — none of these are conditional:

| Control | Flag | What it actually prevents |
|---|---|---|
| No privilege escalation | `--security-opt no-new-privileges` | `setuid`/`sudo` inside the container cannot gain more than it started with |
| No Linux capabilities | `--cap-drop ALL` | Verified by reading `/proc/self/status`'s five `Cap*` lines from inside a running container — all `0000000000000000`, not merely "the flag was passed" |
| Non-root | `--user 1000:1000` (or `config.user`, never the caller's own identity) | A container-breakout primitive that relies on root inside the container gains nothing |
| PID-limit | `--pids-limit {config.resource_limits.pids}` | A kernel cgroup ceiling — verified by actually spawning 40 processes against a limit of 8 and observing `fork` fail |
| Memory limit | `--memory {mb}m` | A kernel OOM-kill — verified by allocating 200MB against a 64MB limit and observing the container die, not merely finish slowly |
| Read-only root + explicit tmpfs | `--read-only`, `--tmpfs {path}` per `config.tmpfs_paths` | The only writable paths are ones explicitly listed — never "everything except what's mounted read-only" |
| No published ports | (no `-p`/`--publish` flag exists anywhere in the provider) | The dev server is never reachable from the host network; health checks use `docker exec`, which works inside the container's own namespace regardless of network policy |
| No Docker socket | (no such mount exists anywhere in the codebase) | A compromised sandbox cannot control the Docker daemon that's running it |
| No `--privileged`, `--network host`, `--pid host`, `--ipc host` | (never constructed) | Statically asserted against the real argv, not just described — see `test_dangerous_flags_never_appear_in_the_constructed_argv` |

## Filesystem isolation

The sandbox's mounts are `Mount(host_path, container_path, read_only)` tuples
built by the caller (`RuntimeManager`, `SandboxValidationRunner`) directly
from an already-hardened `redstone.workspace.manager.Workspace.project_root`
— **no new path-resolution logic exists anywhere in `redstone.sandbox` or
`redstone.runtime`.** The Phase 2A/2A.1 filesystem boundary (traversal,
symlink/junction, hardlink, ADS, Windows-alias defenses — `SECURITY.md` rows
1–2c) is reused unchanged, not reimplemented at a second layer where it could
drift out of sync. Verified directly: a sibling directory outside the mount,
containing a "secret," is unreadable from inside a running sandbox
(`test_filesystem_cannot_reach_outside_the_mount`).

## Environment isolation

```python
class SandboxConfig:
    environment: dict[str, str] = {}   # the COMPLETE environment. No inherit-host flag exists.
```

`DockerSandboxProvider` merges exactly two things into a container's
environment: a small fixed `_BASE_ENV` (`HOME`, `npm_config_cache`, `PATH` —
none of them secrets) and `config.environment`. **There is no line in this
file, or anywhere in `redstone.runtime`, that reads `os.environ`.** BYOK and
server-configured provider credentials live in `redstone.ai.AIConfig`, which
`RuntimeManager` never imports or holds a reference to — not "we don't pass
the key" as an assertion, but structurally: there is no code path by which it
could. Verified by listing the full environment from inside a running
container with zero explicit overrides and confirming only image-baked and
`_BASE_ENV` values appear (`test_environment_contains_no_host_or_secret_variable`),
and again through the full `RuntimeManager` (not just the raw provider) in
`test_full_lifecycle_against_real_docker`.

## Network policy

```
NetworkPolicy.DENY          --network none     no network device at all
NetworkPolicy.INSTALL_ONLY  --network bridge   outbound only, no published ports
NetworkPolicy.ALLOWLIST     NOT IMPLEMENTED — raises UNSUPPORTED_OPERATION if selected
NetworkPolicy.FULL          NOT IMPLEMENTED, and never the default
```

`DENY` is the default and the **only** policy ever used for the long-running
dev server. `INSTALL_ONLY` exists for exactly one purpose — dependency
resolution — and is applied only to the ephemeral install sandbox, which is
destroyed the moment `npm install` finishes. This is a genuine two-network
model, not a documentation label: install and run are two separate
containers with two separate policies, never one long-lived container with a
network policy that changes mid-life.

Verified with real traffic, not synthetic targets: `wget` to a real host
fails outbound under `DENY`
(`test_network_deny_blocks_outbound`); DNS resolution and a **real `npm
install` of a real published package** (`is-odd@3.0.1`) succeed under
`INSTALL_ONLY` against the actual npm registry
(`test_real_npm_install_succeeds_over_install_only_network`) — chosen after
an early attempt against `example.com`/a raw IP gave ambiguous results that
turned out to be an unreliable test target, not a defect in the policy; see
the git history for that investigation.

**Known gap, not hidden:** `INSTALL_ONLY` is unrestricted outbound bridge
networking, not scoped to the npm registry. `ALLOWLIST` (an egress proxy)
would close this and is declared in the model specifically so adding it later
doesn't require an architecture change — it just isn't built yet.

## Resource limits: requested vs. enforced vs. observed

```python
class ResourceLimits:
    cpu_cores: float = 1.0
    memory_mb: int = 512
    pids: int = 128
    timeout_seconds: float = 120.0
    output_bytes: int = 256 * 1024

class SandboxStatus:
    enforced_limits: tuple[str, ...]   # which of the above this provider actually enforces
```

A provider that cannot enforce a limit must say so via `enforced_limits`
rather than silently accepting and ignoring it. For `DockerSandboxProvider`,
three of these were driven to their actual limit and observed to fail
correctly, not merely "the flag was passed":

| Limit | How it was proven | Test |
|---|---|---|
| `pids` | Spawned 40 processes against a limit of 8; observed `fork` fail | `test_pids_limit_bounds_a_fork_bomb` |
| `memory_mb` | Allocated 200MB against a 64MB limit; observed the container OOM-killed | `test_memory_limit_is_enforced` |
| `timeout_seconds` | A real parent→child→grandchild `sleep` tree (3 processes, confirmed running via `ps aux`), then `wait(timeout=2)`; confirmed the **entire tree** gone, not just the parent | `test_timeout_terminates_the_entire_process_tree` |
| `cpu_cores` | ⚠️ Requested (`--cpus`) and reported as enforced (cgroup CPU quota is a well-established Docker mechanism), but not independently stress-tested to observe throttling the way the three above were driven to failure. | — |
| `output_bytes` | Docker's own log file is size/file-count bounded (`--log-opt`); `_read_bounded` additionally stops reading and closes Redstone's own local reader past `max_bytes`, so the trusted process's own memory is bounded regardless of what the log driver retains | `test_logs_are_bounded` |

The timeout row is the one the whole isolation model rests on: a Python
`subprocess.run(timeout=...)` alone only guarantees the *parent* dies on
timeout, not its descendants. `wait()`'s timeout path calls `self.kill()`,
which is `docker kill` — SIGKILL to PID 1 of the container's own PID
namespace, which the kernel tears down completely, every descendant
included. This was verified directly against a real 3-generation tree, not
inferred from "Docker uses namespaces."

## Two providers

| | `DockerSandboxProvider` | `LocalProcessSandboxProvider` |
|---|---|---|
| `name` | `"docker"` | `"local_process"` |
| `is_isolated` | `True` | `False` |
| What it actually is | Real Linux namespaces/cgroups/capabilities via the Docker CLI (argv lists, never a shell string) | `subprocess.Popen`, same OS user/filesystem/network as the Redstone process itself |
| When it's selected | `best_available_provider()` whenever `docker_available()` | Only when Docker isn't reachable |
| Purpose | The actual security boundary | Docker-less local development and fast, portable tests of pure lifecycle logic — **never** a substitute for isolation |

`best_available_provider()` never silently claims isolation it can't provide:
`/api/health` reports `runtime.isolated` from whichever provider is actually
active, and `LocalProcessSandboxProvider.is_isolated = False` is a class
attribute a caller can check, not a comment.

`LocalProcessSandboxProvider`'s environment handling mirrors the Docker
provider's discipline even without real isolation: `Popen(..., env=dict(
config.environment))` — never merged with `os.environ`. Its known,
documented gap is process-tree termination: POSIX gets `start_new_session=True`
and `os.killpg`; Windows gets `CREATE_NEW_PROCESS_GROUP`, which — unlike
Docker's namespace teardown — does **not** reliably reach orphaned
descendants. Stated plainly because this provider is not the security
boundary anyway; see "Two providers" above.

## Testing

`tests/redstone/test_sandbox.py` — 35 tests, all passing (verified in two
separate runs: 16 local-only in ~34s, 19 Docker-dependent in ~42s):

| Group | Covers |
|---|---|
| Command/config/registry unit tests | fixed table lookup, `UNSUPPORTED_OPERATION` for an unknown framework, `ALLOWLIST`/`FULL` rejected at construction, at-least-one-mount enforced |
| `TestLocalProcessProvider` | lifecycle, environment-explicit-only, nonexistent-binary failure, timeout + kill, output truncation, idempotent stop/kill/destroy |
| `TestDockerProviderRealIsolation` | every row in the "Security posture" and "Resource limits" tables above, against a real daemon — skipped with an explicit reason (`Docker daemon not reachable`) rather than mocked, when no daemon is present |
| `test_dangerous_flags_never_appear_in_the_constructed_argv` | monkeypatches `subprocess.run` to capture the real `docker create` argv without creating anything, and asserts none of `--privileged`/`--network host`/`--pid host`/`--ipc host`/`docker.sock` ever appear, while `--cap-drop ALL`/`--security-opt no-new-privileges` always do |

`SandboxValidationRunner` (the Phase 3 `ValidationRunner` seam's concrete
sandboxed implementation) has its own suite,
`tests/redstone/test_sandbox_validation.py` — fake-provider tests for the
PASSED/FAILED/TIMEOUT/UNAVAILABLE mapping, plus two real-Docker tests against
the fixture project in `tests/redstone/fixtures/react_vite_ts_min/`.

## Known limitations

- **`NetworkPolicy.ALLOWLIST`/`FULL` are not implemented** — declared in the
  model so adding them later needs no architecture change, but selecting
  either today raises `UNSUPPORTED_OPERATION`.
- **`INSTALL_ONLY` egress is unrestricted**, not scoped to the npm registry —
  see "Network policy" above.
- **CPU throttling was not independently stress-tested** — see "Resource
  limits" above.
- **The Docker image is pinned to a tag (`node:20-alpine`), not a digest.**
  Documented as a production-readiness gap, not hidden: a tag can be
  repointed upstream; a digest cannot.
- **The cloud metadata endpoint (`169.254.169.254`) was not literally
  reproduced** — this development environment isn't a cloud host. The
  mechanism that would block it (`--network none`, no device at all) is the
  same one proven generally in "Network policy" above.
- **`LocalProcessSandboxProvider` is not a security boundary**, by design —
  see "Two providers" above.
