# Redstone runtime manager

Phase 5. `RuntimeManager` (`redstone.runtime.manager`) owns the full
lifecycle of one project's dev server: create, start (install if needed, then
run), health-check, stop, kill, restart, destroy. It is the only thing above
`redstone.sandbox` that calls a `SandboxProvider` directly — see
`docs/redstone/SANDBOX.md` for what it's actually driving.

> **`RuntimeManager` is not a shell executor.** It never builds a command
> string. Every sandbox it creates runs one `SandboxCommand(Operation,
> Framework)`, resolved through the sandbox layer's own fixed table. Adding
> execution power here did not, and must not, give the Phase 3 coding agent
> any new execution power — see "Agent integration" below.

## Architecture

```
RuntimeManager
   ├── _runtimes: dict[runtime_id, Runtime]            in-memory; not persisted
   ├── _workspace_active: dict[workspace_id, runtime_id]   one active runtime per workspace
   ├── _op_locks: dict[runtime_id, threading.Lock]      serializes mutating calls per runtime
   │
   ├── create(project_id, workspace, framework) -> Runtime
   ├── start(runtime_id, project_id, workspace) -> Runtime
   ├── health_check(runtime_id, project_id) -> bool
   ├── stop / kill(runtime_id, project_id) -> Runtime
   ├── destroy(runtime_id, project_id=None) -> None
   ├── restart(runtime_id, project_id, workspace) -> Runtime
   ├── reconcile() -> int              # sweep runtimes whose sandbox no longer matches recorded state
   └── list_for_project / get(runtime_id, project_id) -> Runtime
```

`Runtime` (the domain object; shape lives in `redstone.domain.models`, kept
framework/HTTP-independent) carries `sandbox_id`/`provider_name` for
`RuntimeManager`'s own bookkeeping. `Runtime.to_dict()` — the only form the
API ever returns — omits both, along with any host path or container id.

Two locks, two different jobs, mirroring the distinction already established
in `AgentService`:

* **`_op_locks[runtime_id]`** serialises every mutating call (`start`,
  `stop`, `kill`, `destroy`, `restart`, the crash-handling path inside
  `health_check`) on the *same* runtime — this is what makes the concurrency
  races below safe.
* **`_workspace_active`** is the *admission* slot: checked-and-set atomically
  under a short-held lock, enforcing one active runtime per workspace, the
  same non-blocking-immediate-rejection pattern `AgentService` already uses
  for `WORKSPACE_BUSY`.

## State machine

```
CREATED ──start──► STARTING ──(healthy)──► RUNNING ──┬─idle──► IDLE ──┐
   │                  │                       │       └────run───────┘
   │                  │                       │
 stop               (any)                  stop
   │                  │                       │
   ▼                  ▼                       ▼
STOPPING ◄─────────────────────────────────────
   │
   ▼
STOPPED ──┐
FAILED  ──┼──destroy──► DESTROYED   (terminal; no further transition)
KILLED  ──┘
EXPIRED ──────────────────────────► (terminal; no further transition)
```

`can_transition_runtime()` (`redstone/runtime/models.py`) is the single
choke point every transition goes through — `RuntimeManager` never calls
`Runtime.with_state()` directly; it calls `transition()`, which raises
`RUNTIME_INVALID_TRANSITION` rather than silently applying an illegal move.
Two distinct ideas, deliberately not conflated:

* **`TERMINAL_RUNTIME_STATES`** (`STOPPED`, `FAILED`, `KILLED`, `DESTROYED`,
  `EXPIRED`) — "no longer the workspace's active runtime." `RuntimeManager`
  releases the one-active-runtime slot the moment a runtime reaches any of
  these.
* **`_FULLY_TERMINAL`** (`DESTROYED`, `EXPIRED` only) — "cannot transition at
  all." `STOPPED`/`FAILED`/`KILLED` are "resting" states that can still move
  on to `DESTROYED` — an explicit cleanup action — which is exactly what
  makes `destroy()` on an already-stopped or already-failed runtime safe
  instead of an illegal-transition error.

This distinction was caught and fixed during design, before it became a bug
in `RuntimeManager`: an earlier draft treated every `TERMINAL_RUNTIME_STATES`
member as fully blocking, which would have made `destroy(already_stopped)`
illegally raise — directly violating the idempotency requirement below.

`destroy()` **tombstones** the record (state → `DESTROYED`) rather than
deleting it from `_runtimes`. `manager.get()` on a destroyed runtime still
returns it — with `state == "destroyed"` — rather than `RUNTIME_NOT_FOUND`.
This is what lets `restart()` report "historical generations" (see below)
and lets a caller distinguish "never existed" from "existed and was
destroyed."

## Startup sequence

`start()` never marks a runtime `RUNNING` merely because a process was
spawned:

1. If the workspace has a `package.json`, run `INSTALL_DEPENDENCIES` in an
   ephemeral sandbox (`NetworkPolicy.INSTALL_ONLY`), bounded by the
   operation's own max timeout; a nonzero exit fails the runtime with
   `RUNTIME_START_FAILED` and the diagnostic output.
2. Create and start the long-running `START_DEV_SERVER` sandbox
   (`NetworkPolicy.DENY`).
3. Poll a bounded loop: on each tick, check `provider.status()` first (catches
   a crash *during* startup — the sandbox going to a terminal state before any
   health probe ever succeeds) and only then probe
   `exec_in(('wget', ..., 'http://127.0.0.1:5173'))`. Only a successful probe
   response ends the loop successfully; a status change to a terminal state
   ends it as a failure immediately, without waiting out the rest of
   `max_startup_seconds`.
4. Only once a health probe actually succeeds does the runtime move to
   `RUNNING`. Any failure along the way tears the sandbox down
   (`kill()` then `destroy()`) before the runtime is marked `FAILED` — proven
   directly: `test_start_fails_when_the_dev_server_never_becomes_healthy`
   asserts the dead sandbox id is gone from the provider's live set, not just
   that the exception was raised.

## Health checks: four levels, not one boolean

`health_check()` distinguishes what a single "is it up" boolean would hide:

1. **Process exists** — `provider.status()` reports the sandbox's own state.
2. **Sandbox not crashed** — a non-`RUNNING` status transitions the runtime
   to `FAILED` immediately, with bounded diagnostics (`provider.logs()`,
   capped) attached to `last_error`.
3. **Responsive** — an actual `exec_in()` probe (`wget` against
   `127.0.0.1:5173`, run **inside** the sandbox's own namespace, which is why
   this works even though the sandbox's own `NetworkPolicy` is `DENY`).
4. **Serving content** — the probe's exit code, not merely whether the
   process is alive; a container that's up with a hung server correctly
   reports unhealthy, not healthy.

`reconcile()` runs the same crash-detection logic across every non-terminal
runtime `RuntimeManager` still holds a record for — the sweep that catches a
crash between on-demand health checks (see Known limitations for what it does
*not* catch).

## Idempotency

| Operation on... | Behavior |
|---|---|
| `start()` an already-`RUNNING` runtime | No-op; returns the existing runtime unchanged, no second sandbox created |
| `stop()` an already-terminal runtime | No-op; returns the existing runtime unchanged |
| `stop()` a `CREATED` (never-started) runtime | Transitions directly to `STOPPED` — there is nothing to gracefully stop, but this must not raise `RUNTIME_INVALID_TRANSITION` (a real gap the state machine had until this was tested — see State machine above) |
| `kill()` an already-terminal runtime | No-op |
| `destroy()` an unknown runtime id | No-op (nothing to destroy) |
| `destroy()` an already-`DESTROYED` runtime | No-op |
| `restart()` a `FAILED` runtime | Destroys the failed generation, creates and starts a fresh one — "restart to recover from failure" is a first-class, tested path, not an edge case |

## Restart semantics

`restart()` = **destroy the current generation, then create and start a new
one** (a new `Runtime.id`) for the same `project_id`/`workspace` — not an
attempt to move a terminal state back to `CREATED`. This was a deliberate
choice among alternatives, made to sidestep re-entering a state machine that
otherwise has no legal path backward out of a terminal state, and justified
directly by this phase's own brief, which offered "one active runtime plus
historical metadata" as an acceptable model. The old generation's record
stays retrievable (tombstoned, `state == DESTROYED`) via `list_for_project()`
— it is history, not noise to discard.

The sequence guards against a real race, not just the happy path: after
destroying the old generation, `restart()` re-checks that the workspace's
active-runtime slot is actually free (guarding against a concurrent
`create()` elsewhere winning it in the gap) before creating the new
generation; `create()` itself repeats the same atomic check-and-set, so the
race is closed twice, not once. Any failure during the new generation's
`start()` leaves it `FAILED` with its own slot released — never
"ambiguously running."

## Concurrency races — tested, not assumed safe

| Race | How it's proven safe |
|---|---|
| Simultaneous `create()` for the same workspace | 8 threads race; exactly 1 succeeds, 7 get `RUNTIME_BUSY` — `test_simultaneous_creates_for_the_same_workspace_only_one_wins` |
| `start()` + `stop()` on the same runtime | 10 randomized iterations; final state is always `RUNNING` or `STOPPED`, never left mid-transition — `test_start_and_stop_race_leaves_a_consistent_terminal_state` |
| Simultaneous `destroy()` | 6 threads race; zero exceptions escape, final state is `DESTROYED` — `test_simultaneous_destroys_are_all_safe` |
| `restart()` racing `destroy()` | At most one runtime ends up in an active (non-terminal) state for the workspace afterward, whichever operation "won" — `test_restart_and_destroy_race_never_leaves_two_active_generations` |

The same suite runs again against the **real Docker provider**
(`tests/redstone/test_runtime_docker_integration.py`) for the sequential
lifecycle: create → install → start → health-check → environment/network
assertions (re-verifying, through `RuntimeManager` this time, that nothing
Phase 4 proved was weakened) → stop → destroy → confirm zero orphaned
`redstone-`-prefixed containers remain.

## Agent integration

**No new tool was added to `ToolRegistry`.** `run_typecheck`/`run_lint`/
`run_build` are unchanged as tools — they still only ever select one of the
same fixed `Operation` values. What's new is `redstone.runtime.
SandboxValidationRunner`, a concrete implementation of the Phase 3
`ValidationRunner` protocol (structural, not inherited — `redstone.runtime`
needs no import from `redstone.agent`) that runs each call as one bounded
sandboxed execution (`create → start → wait → destroy`, `NetworkPolicy.DENY`
— none of typecheck/lint/build need network), exactly mirroring
`RuntimeManager._run_install`'s own shape. It does **not** run an install
phase itself: a missing `node_modules` produces an honest `FAILED`/
`UNAVAILABLE` result, not a silent auto-install that could race with
`RuntimeManager`'s own install handling of the same workspace.

`AgentService`'s default `ValidationRunner` is still `UnavailableValidationRunner`
— a composition root (e.g. `redstone/api/app.py`) must explicitly construct
and inject a `SandboxValidationRunner` to get real results. Verified:
`PASSED` only on a real exit 0, `FAILED` on nonzero exit or timeout,
`UNAVAILABLE` (never a fabricated `PASSED`) if the provider itself can't be
reached — including a real-Docker case where `tsc` genuinely isn't installed
in the fixture project (`test_typecheck_fails_honestly_when_tsc_is_not_installed`).

## Error taxonomy

`RuntimeErrorCode` (`redstone/runtime/errors.py`), following the same
Redstone-authored-safe-message pattern as `AgentErrorCode`/`AIErrorCode`/
`SandboxErrorCode`:

`RUNTIME_NOT_FOUND`, `RUNTIME_NOT_OWNED`, `RUNTIME_BUSY`,
`RUNTIME_CREATE_FAILED`, `RUNTIME_START_FAILED`, `RUNTIME_STOP_FAILED`,
`RUNTIME_TIMEOUT`, `RUNTIME_RESOURCE_LIMIT`, `RUNTIME_NETWORK_DENIED`,
`RUNTIME_HEALTHCHECK_FAILED`, `RUNTIME_DESTROY_FAILED`, plus
`RUNTIME_INVALID_TRANSITION`/`RUNTIME_INVALID_REQUEST` for internal
consistency with the rest of the codebase's error classes. `.internal`
carries server-only diagnosis (e.g. sandbox stdout) and is never rendered by
`to_dict()`.

## Event model

`RuntimeManager` publishes through the same `EventBus` as the agent loop
(`redstone.events.bus`), with the same forbidden-key stripping applied at
publish time: `runtime.created`, `.starting`, `.started`, `.health_check`,
`.crashed`, `.stopping`, `.stopped`, `.killed`, `.destroyed`, `.failed`.
Payloads carry `runtime_id` plus small safe scalars (error codes, sandbox
state names, diagnostic byte counts) — never file content, never a
credential, never a host path.

## API

```
POST /api/projects/{project_id}/runtime           create (409 RUNTIME_BUSY if one is already active)
GET  /api/projects/{project_id}/runtime           current/most-recent runtime for this project
POST /api/projects/{project_id}/runtime/start     create-if-needed, then start
POST /api/projects/{project_id}/runtime/stop
POST /api/projects/{project_id}/runtime/restart
```

Every handler resolves `project_id -> Project -> Workspace` through
`AgentService.get_workspace()` — the one place outside `AgentService` allowed
to reach a project's real filesystem path. **No request body anywhere has a
`workspace_id` or `runtime_id` field**; a client addresses a project, the
server looks up the current runtime and its workspace internally. Every
response is `Runtime.to_dict()` — no `sandbox_id`, no `provider_name`, no
container id, no host path, ever. `RUNTIME_NOT_OWNED` and `RUNTIME_NOT_FOUND`
both map to HTTP 404, deliberately: confirming that a runtime id exists but
belongs to someone else is its own small information leak.

`GET /api/health` reports `runtime.provider` and `runtime.isolated` — an
honest, unhidden statement of whether the active `SandboxProvider` actually
provides isolation right now, not an assumption baked into the response.

## Ownership chain

```
Project ─owns─► Workspace ─owns─► Runtime ─owns─► Sandbox ─owns─► Process
```

Cleanup runs bottom-up on the happy path (`destroy()` tears the sandbox down
before tombstoning the runtime record). Failure recovery reconciles out of
order where it must: `reconcile()` and every in-process failure path
(`start()`'s except blocks, `health_check()`'s `_mark_crashed`) can mark a
`Runtime` `FAILED`/release its workspace slot even when the underlying
sandbox is already gone — it does not require the ownership chain to unwind
cleanly to notice and correct an inconsistency.

## Testing

`tests/redstone/test_runtime.py` — 33 tests against `FakeSandboxProvider`
(`tests/redstone/runtime_fakes.py`), covering lifecycle, ownership,
idempotency, crash detection, restart, and every concurrency race listed
above — the one layer this phase's testing policy allows mocking, per the
same "mock only at the outermost provider seam" rule already applied to the
AI gateway (Phase 2B) and now the sandbox provider (Phase 4).

`tests/redstone/test_runtime_docker_integration.py` — 4 tests against the
**real** `DockerSandboxProvider`: full lifecycle, real crash detection
(`provider.kill()` out-of-band, then `health_check()` catches it), restart
replacing the container, and a start-failure path that leaves zero orphaned
containers.

`tests/redstone/test_runtime_api.py` — 12 tests over the HTTP surface
(`FastAPI TestClient`), including the defensive sweep that no response ever
contains `sandbox_id`, `provider_name`, the word "container", or a host
temp-directory path.

`tests/redstone/test_sandbox_validation.py` — 8 tests for
`SandboxValidationRunner` (6 against a fake provider, 2 against real Docker).

## Known limitations

- **`reconcile()` only reconciles runtimes it still holds an in-memory
  record for.** `_runtimes`/`_workspace_active` are not persisted; a
  container orphaned by a full Redstone process restart is not
  automatically found. Every *in-process* failure path does tear its own
  sandbox down — this is a gap in surviving a process restart, not in
  ordinary failure handling.
- **Health checks are on-demand or via `reconcile()`, not a background
  poller.** Nothing currently calls `health_check()`/`reconcile()`
  periodically on its own; a caller (the API, a future scheduler) must
  invoke them.
- **One active runtime per workspace is a fixed policy, not configurable.**
  Chosen because a project has exactly one dev server in this phase's scope;
  revisiting it (e.g. preview + a background build server) is future work,
  not silently precluded by the data model (`Runtime.workspace_id` is just a
  field) but not built.
- **`SandboxValidationRunner` inherits every sandbox-layer limitation** in
  `docs/redstone/SANDBOX.md`'s Known limitations — notably that
  `NetworkPolicy.DENY` for typecheck/lint means `npx --no-install` can take
  significantly longer to fail than to succeed when a tool genuinely isn't
  installed (observed: ~86s, still bounded by the 120s `TYPECHECK` ceiling,
  never a hang) — DNS-resolution fallback behavior inherent to `npx`
  combined with no network device, not a defect in `RuntimeManager` or the
  sandbox.
