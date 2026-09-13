# Redstone runtime manager

Phase 5, hardened in Phase 5.1. `RuntimeManager` (`redstone.runtime.manager`)
owns the lifecycle of one project's dev server: create, start (install if
needed, then run), health-check, stop, kill, restart, destroy — and, since
5.1, recovery from a Redstone process restart. It is the only thing above
`redstone.sandbox` that drives a `SandboxProvider` for runtimes; see
`SANDBOX.md` for what that provider actually enforces.

> **`RuntimeManager` is not a shell executor.** Every sandbox it creates runs
> one `SandboxCommand(Operation, Framework)` from the sandbox layer's fixed
> table. The coding agent gained no execution power from this layer — see
> Agent integration.

## Architecture

```
RuntimeManager
   _runtimes: dict[runtime_id, Runtime]                in memory, not persisted
   _workspace_active: dict[workspace_id, runtime_id]   one active runtime per workspace
   _op_locks: dict[runtime_id, Lock]                   serialises mutating calls per runtime

   create / start / health_check / logs / stop / kill / destroy / restart
   reconcile()                      in-memory runtimes whose sandbox died
   reconcile_orphaned_containers()  Redstone-owned sandboxes this process does
                                    not track -- survives a restart (5.1)
```

`Runtime.to_dict()` — the only shape the API returns — never includes
`sandbox_id`, `provider_name`, a container id or a host path.

## State machine

```
CREATED ──start──► STARTING ──healthy──► RUNNING ──stop──► STOPPING ──► STOPPED
   │                  │                     │
   └──stop──► STOPPING│ failure/crash       │ crash / quota kill (health_check, reconcile)
                      ▼                     ▼
                    FAILED               FAILED
any non-terminal ──kill──► KILLED
STOPPED | FAILED | KILLED ──destroy──► DESTROYED   (terminal)
```

Every transition goes through `can_transition_runtime()` / `transition()`.
`TERMINAL_RUNTIME_STATES` (`STOPPED`, `FAILED`, `KILLED`, `DESTROYED`,
`EXPIRED`) means "no longer the workspace's active runtime"; only `DESTROYED`
and `EXPIRED` refuse every further transition, which is what makes
`destroy()` on a stopped or failed runtime a safe no-op. `destroy()`
tombstones the record rather than deleting it.

**Reserved states (5.1):** `IDLE` and `EXPIRED` are in the enum and the
transition graph so idle-suspend and TTL expiry can be added without
reshaping it, but **no code path assigns either**. A runtime observed through
the API is never in one of them. Marked as such in `domain/models.py`.

## Startup

1. If the workspace has `package.json`, run `INSTALL_DEPENDENCIES` in an
   ephemeral sandbox (`NetworkPolicy.INSTALL_ONLY`: an `--internal` network
   whose only way out is a per-install egress proxy that allows the configured
   registry and nothing else — see `SANDBOX.md`), destroyed along with its
   proxy and networks in a `finally`. If the proxy can't start, the install
   fails with `RUNTIME_CREATE_FAILED` — there is no fallback network.
2. Create and start the `START_DEV_SERVER` sandbox (`NetworkPolicy.DENY`).
3. Poll: check `provider.status()` first (catches a crash — or a quota kill —
   during startup), then probe `http://127.0.0.1:5173` via `exec_in` inside
   the sandbox's own namespace.
4. Only a successful probe moves the runtime to `RUNNING`. Any failure tears
   the sandbox down before the runtime is marked `FAILED`.

Every sandbox carries ownership labels: `redstone.project_id`,
`redstone.workspace_id`, `redstone.runtime_id` and `redstone.purpose` (the
operation), plus the provider's own `redstone.managed=true`. Opaque ids only.

## Health checks

1. Process exists (`provider.status()`).
2. Not crashed / not killed — otherwise `FAILED`, with bounded diagnostics.
   If the sandbox was killed by a resource-limit watchdog the error is
   `RUNTIME_RESOURCE_LIMIT`, not `RUNTIME_HEALTHCHECK_FAILED`.
3. Responsive (`exec_in` probe).
4. Serving — the probe's exit code, not just liveness.

`reconcile()` applies the same crash detection to every non-terminal runtime
this process holds in memory.

## Orphan recovery (Phase 5.1)

**The problem.** `_runtimes` lives in memory. If the Redstone process dies,
its containers keep running, but nothing remembers them.

**The mechanism.** Every container carries `redstone.managed=true` and the
runtime's ids as Docker labels, which live on the container and survive the
process. `SandboxProvider.list_managed()` asks Docker directly (`docker ps -a
--filter label=redstone.managed=true`, then `docker inspect`) — it never
consults memory.

**The policy — destroy, never adopt.** `reconcile_orphaned_containers()`
destroys every Redstone-owned sandbox that doesn't belong to a runtime this
process is actively tracking. A restarted process can't know whether a
surviving container finished its install, passed its health check or is
serving the right files; re-adopting it as `RUNNING` would mean trusting
state nobody verified. Destroying it is always safe — the user starts the
project again — and guarantees no duplicate runtime can coexist with a fresh
one for the same workspace.

**What it never touches:**

- Anything without `redstone.managed=true`. Docker's own label filter excludes
  it before Redstone sees it, and `list_managed()` re-checks the label anyway.
- Anything carrying the label but not named `redstone-…` (skipped, counted as
  `skipped_unrecognised`).
- Sandboxes of runtimes this process is actively tracking (`kept`).

**Malformed metadata** — missing ids, or ids that don't match Redstone's own
id format (e.g. `../../bad`) — is counted as `malformed` and destroyed. It
never crashes reconciliation.

**When it runs.** `create_app()` calls it once at startup, whenever it builds
the default `RuntimeManager` (a fresh process tracks nothing, so everything
Redstone-owned is an orphan). It's idempotent and can be called again at any
time. Each orphan's install infrastructure — its egress proxy container
(`<sandbox_id>-proxy`, carrying the same runtime labels) and both networks
(`<sandbox_id>-net`, `<sandbox_id>-egress`) — is removed along with it,
because every name is derived from the sandbox id. Proven by
`test_orphan_recovery_removes_install_infrastructure` (real Docker).

**Proof (real Docker):**
`test_orphaned_runtime_is_recovered_after_a_simulated_process_restart` —
process A creates and starts a real runtime, and its own reconcile keeps it;
A's manager is then discarded (metadata lost, container still running);
process B, a fresh `RuntimeManager` on the same daemon, reconciles:

- A's container is destroyed.
- A malformed Redstone-labelled container is destroyed.
- An unlabelled container is untouched.
- A labelled container without the `redstone-` name is untouched.
- A fresh runtime for the same workspace then starts, and exactly one
  container carries that workspace's label.

**Limits:** reconciliation is triggered at startup, not continuously; it acts
on whatever Docker daemon the provider talks to, so all Redstone processes
sharing a daemon must share its view (a second, concurrently running Redstone
process on the same daemon would treat the first's containers as orphans —
Redstone is single-process today).

## Idempotency

| Operation on… | Behaviour |
|---|---|
| `start()` a `RUNNING` runtime | no-op, same sandbox |
| `stop()` / `kill()` a terminal runtime | no-op |
| `stop()` a `CREATED` runtime | → `STOPPED` |
| `destroy()` an unknown or `DESTROYED` runtime | no-op |
| `restart()` a `FAILED` runtime | new generation, fresh start |
| `reconcile_orphaned_containers()` twice | second call destroys nothing |

## Restart

`restart()` = destroy the current generation, then create and start a new
one (new `Runtime.id`). The slot is re-checked in between so a concurrent
`create()` can't produce a second active runtime.

## Concurrency

| Race | Test |
|---|---|
| 8 simultaneous `create()` | `test_simultaneous_creates_for_the_same_workspace_only_one_wins` |
| `start()` vs `stop()`, 10 iterations | `test_start_and_stop_race_leaves_a_consistent_terminal_state` |
| 6 simultaneous `destroy()` | `test_simultaneous_destroys_are_all_safe` |
| `restart()` vs `destroy()` | `test_restart_and_destroy_race_never_leaves_two_active_generations` |

These test `RuntimeManager`'s own bookkeeping against a fake provider; there
is no real-Docker concurrent stress test.

## Error taxonomy

Since 5.1, every public code is either produced by a real code path or
explicitly documented as reserved (enforced by
`test_every_public_error_code_is_either_raised_or_documented_reserved`):

| Code | Status | Raised when |
|---|---|---|
| `RUNTIME_NOT_FOUND` | live | unknown runtime / nothing to act on |
| `RUNTIME_NOT_OWNED` | live | runtime belongs to another project (HTTP 404, deliberately) |
| `RUNTIME_BUSY` | live | workspace already has an active runtime |
| `RUNTIME_CREATE_FAILED` | **live since 5.1** | sandbox (or its network) couldn't be created, or the provider is unavailable |
| `RUNTIME_START_FAILED` | live | install exited non-zero; other start failures |
| `RUNTIME_TIMEOUT` | **live since 5.1** | install exceeded its timeout; a sandbox CLI call timed out |
| `RUNTIME_RESOURCE_LIMIT` | **live since 5.1** | install or running dev server killed by a resource watchdog (`storage_mb`) |
| `RUNTIME_HEALTHCHECK_FAILED` | live | never became healthy; crashed after `RUNNING` |
| `RUNTIME_STOP_FAILED` | live | provider stop failed |
| `RUNTIME_DESTROY_FAILED` | live | provider destroy failed |
| `RUNTIME_INVALID_TRANSITION` | live | illegal state change |
| `RUNTIME_NETWORK_DENIED` | **RESERVED** | nothing — no egress proxy exists to report a denial, and under `--network none` a blocked connection is indistinguishable from any other failed command |
| `RUNTIME_INVALID_REQUEST` | **RESERVED** | nothing — inputs are typed and resolved server-side |

Before 5.1, `RESOURCE_LIMIT`, `TIMEOUT` and `CREATE_FAILED` were declared but
never raised; every sandbox failure was flattened to `START_FAILED`.

## Events

Emitted: `runtime.created`, `.starting`, `.started`, `.health_check`,
`.crashed`, `.stopping`, `.stopped`, `.killed`, `.destroyed`, `.failed`.

**Reserved, not emitted:** `runtime.output` (streaming isn't implemented;
output is bounded logs on request) and `runtime.error` (failures are
`runtime.failed` / `runtime.crashed`). Marked as reserved in
`domain/models.py`.

## Operator configuration (Phase 5.1)

Sandbox ceilings can be tuned by the deployment, never by a project, API
caller or the agent. Every value is bounded on **both** sides — there is no
value meaning "unlimited" and none that turns a limit off — and anything
unparsable, `NaN`, infinite or out of range **fails closed to the default**
(it is neither clamped nor honoured).

| Variable | Default | Accepted range |
|---|---|---|
| `REDSTONE_SANDBOX_CPU_CORES` | 1.0 | 0.1 – 8.0 |
| `REDSTONE_SANDBOX_MEMORY_MB` | 512 | 64 – 8192 |
| `REDSTONE_SANDBOX_PIDS` | 128 | 16 – 1024 |
| `REDSTONE_SANDBOX_OUTPUT_BYTES` | 262144 | 4 KiB – 4 MiB |
| `REDSTONE_SANDBOX_STORAGE_MB` | 1024 | 16 – 16384 |
| `REDSTONE_RUNTIME_STARTUP_SECONDS` | 60 | 5 – 600 |
| `REDSTONE_RUNTIME_HEALTH_TIMEOUT_SECONDS` | 5 | 1 – 60 |
| `REDSTONE_RUNTIME_STOP_GRACE_SECONDS` | 10 | 1 – 120 |
| `REDSTONE_INSTALL_NETWORK_ENABLED` | true | `false`/`0`/`no` → installs get **no network** (never more); anything unrecognised → default |
| `REDSTONE_INSTALL_REGISTRY_HOSTS` | `registry.npmjs.org` | 1–8 comma-separated DNS names; **all-or-nothing**: one invalid entry (IP, wildcard, URL, port, numeric name) and the default stands |
| `REDSTONE_INSTALL_PROXY_CONNECT_TIMEOUT_SECONDS` | 10 | 1 – 60 |
| `REDSTONE_INSTALL_PROXY_MAX_CONNECTIONS` | 32 | 1 – 256 |

**Every registry host an operator adds is additional internet surface
reachable by arbitrary lifecycle-script code during install.** Add only
registries you actually install from.

Not configurable at all: the runtime image (a digest-pinned constant), network
policy, allowed ports (443), IP or wildcard registry entries, security flags,
user, mounts.

## Agent integration

No tool was added to `ToolRegistry`. `run_typecheck`/`run_lint`/`run_build`
still select a fixed `Operation`. `SandboxValidationRunner` implements the
Phase 3 `ValidationRunner` protocol with one bounded sandboxed execution per
call (`NetworkPolicy.DENY`, storage-capped, labelled `redstone.purpose`), and
reports `PASSED` only on a real exit 0. `AgentService` still defaults to
`UnavailableValidationRunner`; a composition root must inject the sandboxed
runner explicitly.

## API

```
POST /api/projects/{project_id}/runtime           create (409 RUNTIME_BUSY)
GET  /api/projects/{project_id}/runtime
POST /api/projects/{project_id}/runtime/start
POST /api/projects/{project_id}/runtime/stop
POST /api/projects/{project_id}/runtime/restart
```

No request carries a `workspace_id` or `runtime_id`. `GET /api/health`
reports the active provider and whether it isolates.

## Testing

| File | Tests | Kind |
|---|---|---|
| `test_runtime.py` | 33 | fake provider |
| `test_runtime_api.py` | 12 | fake provider, HTTP |
| `test_runtime_docker_integration.py` | 4 | real Docker |
| `test_runtime_hardening.py` | 31 | 30 unit/fake (error wiring, labels, reconcile policy, config bounds), 1 real Docker (restart recovery) |

## Known limitations

- **Orphan recovery destroys rather than adopts** — a restart loses running
  previews by design; the user starts them again.
- **Reconciliation runs at startup, not continuously**, and assumes a single
  Redstone process per Docker daemon.
- **Health checks run on demand** or via `reconcile()`; there is no
  background poller.
- **`_op_locks` grows by one entry per runtime ever created** in a process's
  lifetime — not a security issue at current scale.
- **Concurrency is proven against the fake provider**, not with concurrent real
  Docker operations.
- **Inherits every sandbox-layer limitation** in `SANDBOX.md`, notably
  periodically-enforced storage, resolver trust for allowlisted names, and the
  egress proxy being trusted code. (Unrestricted install-phase internet egress
  was closed in Phase 4.2.)
- **Each install starts its own proxy** (~1.4 s including the sandbox) —
  simple, strongly isolated, not free.
