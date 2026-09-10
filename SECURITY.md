# Redstone — security model

Redstone runs code it did not write: files produced by an AI model, and
packages chosen by a user. The whole design follows from treating that code as
hostile.

> **Status: Phase 2A (foundation).** The workspace, path and secret boundaries
> below are implemented and tested. The runtime, preview, agent and AI layers
> are **not yet built**; their rows say so explicitly. Nothing in this document
> describes a control that does not exist.

## Trust boundary

```
TRUSTED                              UNTRUSTED
────────────────────────────────     ──────────────────────────────
Redstone backend                     AI-generated code
AI gateway + provider credentials     npm packages and their scripts
Workspace manager                     package.json scripts
Snapshot store (.redstone/)           project source files
Configuration and limits              the running dev server
                                      the preview document
```

Untrusted content must never reach the trusted side except as **data**. A
project file is never executed by the backend, never interpolated into a shell
command, and never treated as an instruction to the agent.

## The workspace boundary

Every project owns a workspace:

```
<workspaces_root>/<workspace-id>/
    project/        ← the only directory the agent can address
    .redstone/      ← snapshots and Redstone state; a sibling, not a child
```

`.redstone/` is a **sibling** of `project/` on purpose. Every path the agent
supplies is resolved against `project/`, and `..` is refused, so snapshots are
structurally unreachable from inside the project rather than merely hidden.

## Threats

| # | Threat | Mitigation | Test |
|---|---|---|---|
| 1 | **Path traversal** | `resolve()` rejects `..` outright rather than collapsing it. Collapsing invites disagreement between our normalisation and the OS's. | `test_parent_traversal_is_rejected` (14 cases), `test_traversal_that_lands_back_inside_is_still_rejected` |
| 2 | **Symlink / junction escape** | Containment is checked against `os.path.realpath`, after every link in the chain is resolved. Nested links included. | `test_symlink_escaping_the_workspace_is_rejected`, `test_nested_link_escape_is_rejected`, `test_writing_through_an_escaping_link_is_blocked` |
| 3 | **Absolute / drive / UNC paths** | Rejected before joining: leading `/`, `X:`, `//`, `\\`. Even a correct absolute path into the workspace is refused. | `test_absolute_drive_and_unc_paths_are_rejected` (10 cases) |
| 4 | **NUL and control bytes** | Rejected. NUL truncates paths in C-level calls, so the file opened can differ from the one checked. | `test_null_bytes_are_rejected`, `test_control_characters_are_rejected` |
| 5 | **Encoded traversal** | `%2e`, `%2f`, `%5c`, `%00` rejected. We never URL-decode paths; this guards against a layer that does. | `test_percent_encoded_separators_are_rejected` |
| 6 | **Windows device names** | `CON`, `NUL`, `COM1`… rejected, extension stripped first (`NUL.txt` still names the device). | `test_windows_device_names_are_rejected` |
| 7 | **Cross-project access** | Each workspace resolves against its own root; workspace ids are validated against `^[A-Za-z0-9_-]{3,64}$` before reaching the filesystem. | `test_one_workspace_cannot_reach_another`, `test_malicious_workspace_ids_are_rejected` |
| 8 | **Host path disclosure** | Results and errors carry workspace-relative paths only. Rejection messages name the broken rule, never the resolved path. | `test_error_messages_do_not_disclose_host_paths`, `test_relative_output_never_contains_backslashes` |
| 9 | **Secret exfiltration via file read** | `.env*`, `*.pem`, `*.key`, `id_rsa`, `.npmrc`, `credentials.*` excluded from search, from state capture, and from snapshots. | `test_search_skips_secret_files`, `test_secret_files_are_excluded_from_state`, `test_snapshot_excludes_secrets` |
| 10 | **Secret leakage through events** | The bus strips forbidden keys (`api_key`, `authorization`, `token`, `prompt`, `content`…) and truncates long values, at publish time rather than trusting callers. | `test_forbidden_payload_keys_are_stripped`, `test_long_payload_values_are_truncated` |
| 11 | **Secret leakage through config repr** | `AIConfig.__repr__` and `__str__` print `<set>`/`<unset>`, never the key. Debuggers and exception handlers print reprs. | `test_ai_config_never_reveals_the_key` |
| 12 | **Resource exhaustion — file size** | `max_read_size` / `max_write_size` enforced *before* the write, so an oversized file is never briefly on disk. | `test_read_rejects_oversized_file`, `test_write_rejects_oversized_content` |
| 13 | **Resource exhaustion — project growth** | `max_project_size` and `max_files` checked on every write. | `test_write_enforces_project_size_limit`, `test_write_enforces_file_count_limit` |
| 14 | **Resource exhaustion — traversal depth** | `max_directory_depth` prunes `os.walk`; ignored trees (`node_modules`, `.git`) never descended. | `test_list_files_skips_node_modules` |
| 15 | **Regex denial of service** | Search is plain substring, not regex. A model-supplied pattern is untrusted input and a pathological regex is a DoS. | `test_search_rejects_empty_query` |
| 16 | **Snapshot tampering / secret revival** | Snapshots live outside the agent-reachable tree and exclude secrets, so a rollback cannot resurrect a credential. | `test_snapshot_lives_outside_the_agent_reachable_project`, `test_snapshot_excludes_secrets` |
| 17 | **Rollback affecting another project** | The store is constructed per workspace and only ever writes under its own `project/`. | `test_rollback_only_touches_its_own_project` |
| 18 | **Fabricated change reporting** | Changesets are computed by hashing the filesystem before and after, never from the model's account. Unannounced edits still appear. | `test_changeset_reports_the_real_filesystem_not_a_claim` |
| 19 | **Arbitrary command execution** | ⏳ **Not yet applicable.** No subprocess execution exists in Phase 2A. `grep -rn "subprocess\|shell=True\|eval(\|exec(" src/redstone/` returns nothing. | verified by inspection |
| 20 | **Dependency install on the trusted host** | ⏳ **Not mitigated — see Known risks.** No install path exists yet, and none may be added outside an isolated runtime. | — |
| 21 | **Preview breakout / XSS** | ⏳ **Not yet built.** Planned: separate origin, sandboxed iframe, CSP. | — |
| 22 | **SSRF / internal network access** | ⏳ **Not yet built.** Planned: no network by default, explicit allowlist. | — |
| 23 | **Prompt injection from project files** | ⏳ **Not yet built.** Planned: repository content passed as data with a fixed system prompt that content cannot override. | — |
| 24 | **Provider error leaking credentials** | ⏳ **Not yet built.** Planned: normalise every upstream failure into `ErrorModel`; never surface raw exception text. | — |

## Known risks, stated plainly

**A local process runtime is not a security boundary.** When the runtime layer
is built, a `LocalProcessRuntime` will run project code as the same OS user,
with the same filesystem and network reach, as the backend. `npm install`
executes arbitrary `postinstall` scripts. That is acceptable for local
development and **not acceptable in production**. Production requires container
or VM isolation. This is recorded here rather than discovered later.

**Docker is unavailable in the current environment.** The CLI is installed but
the daemon is not running, so container isolation cannot presently be tested.

**The npm registry is unreachable** from the current environment, so dependency
installation cannot be exercised at all. The zero-dependency starter exists to
prove the build/run/preview loop without it.

**TOCTOU on path resolution.** `resolve()` checks containment and then returns a
path the caller opens. A link created in that window could in principle redirect
the open. Mitigating fully needs `O_NOFOLLOW`-style handle-based operations,
which are not portable to Windows. The exposure requires an attacker already
able to write into the workspace concurrently.

**Secret detection is heuristic.** Filename matching catches the common cases.
Content scanning is not yet implemented, so a credential pasted into
`src/config.ts` would not be excluded.

## Verifying the boundary

```bash
python -m pytest tests/redstone/test_paths.py -q      # 82 escape attempts
python -m pytest tests/redstone/ -q                   # full foundation suite

# The core must never execute anything or import a framework:
grep -rnE "subprocess|shell=True|eval\(|exec\(|os\.system" src/redstone/
grep -rnE "^\s*(import|from)\s+(fastapi|httpx|requests)" src/redstone/
```

## Reporting

This is a student project under active development and is **not production
software**. Do not deploy it where untrusted users can reach it until the
runtime isolation described above actually exists.
