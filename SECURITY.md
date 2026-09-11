# Redstone — security model

Redstone runs code it did not write: files produced by an AI model, and
packages chosen by a user. The whole design follows from treating that code as
hostile.

> **Status: Phase 2A + 2A.1 (foundation, hardened).** The workspace, path and secret boundaries
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
| 2 | **Symlink / junction escape** | Containment is checked against `os.path.realpath`, after every link in the chain is resolved. Nested links included. Directory walks (listing, sizing, snapshot, restore) never descend a reparse point, so a planted junction cannot expose an external tree. | `test_symlink_escaping_the_workspace_is_rejected`, `test_nested_link_escape_is_rejected`, `test_project_size_does_not_walk_a_junction`, `test_snapshot_ignores_a_junction` |
| 2a | **Hardlink escape** (2A.1) | `read_file`/`write_file`/`rename_file` refuse any regular file with `st_nlink > 1`; a snapshot copies content, de-linking it. A hardlink's data may live outside the workspace, and `realpath` cannot detect one. **This hardens the Redstone FS layer only; it is not a substitute for OS isolation of running project code, which can create hardlinks freely.** | `test_hardlink_read_escape_is_blocked`, `test_hardlink_write_escape_is_blocked_and_outside_unchanged`, `test_hardlink_rename_is_blocked`, `test_hardlink_in_nested_directory_is_blocked` |
| 2b | **NTFS alternate data streams** (2A.1) | A `:` in any path component is rejected, so `a.txt:hidden` cannot hide bytes from size accounting, snapshots or change detection. | `test_ntfs_alternate_data_streams_are_rejected` |
| 2c | **Windows filename aliases** (2A.1) | A trailing dot or space on an interior component is rejected; the secret filter and canonical-name check strip trailing dots/spaces and resolve 8.3 short names, so `id_rsa.`, `id_rsa ` and `CREDEN~1.JSO` cannot slip a secret past the filter. | `test_trailing_dot_or_space_components_are_rejected`, `test_secret_8_3_alias_is_blocked`, `test_case_variant_secret_is_blocked` |
| 3 | **Absolute / drive / UNC paths** | Rejected before joining: leading `/`, `X:`, `//`, `\\`. Even a correct absolute path into the workspace is refused. | `test_absolute_drive_and_unc_paths_are_rejected` (10 cases) |
| 4 | **NUL and control bytes** | Rejected. NUL truncates paths in C-level calls, so the file opened can differ from the one checked. | `test_null_bytes_are_rejected`, `test_control_characters_are_rejected` |
| 5 | **Encoded traversal** | `%2e`, `%2f`, `%5c`, `%00` rejected. We never URL-decode paths; this guards against a layer that does. | `test_percent_encoded_separators_are_rejected` |
| 6 | **Windows device names** | `CON`, `NUL`, `COM1`… rejected, extension stripped first (`NUL.txt` still names the device). | `test_windows_device_names_are_rejected` |
| 7 | **Cross-project access** | Each workspace resolves against its own root; workspace ids are validated against `^[A-Za-z0-9_-]{3,64}$` before reaching the filesystem. | `test_one_workspace_cannot_reach_another`, `test_malicious_workspace_ids_are_rejected` |
| 8 | **Host path disclosure** | Results and errors carry workspace-relative paths only. Rejection messages name the broken rule, never the resolved path. | `test_error_messages_do_not_disclose_host_paths`, `test_relative_output_never_contains_backslashes` |
| 9 | **Secret exfiltration via file read** | `read_file`/`rename_file` **refuse** `.env*`, `*.pem`, `*.key`, `id_rsa`, `id_ed25519`, `.npmrc`, `credentials.*`, `secrets.*` (rejection, not redaction), checking both the requested name and the canonical on-disk name. Also excluded from search, state capture and snapshots. Central policy in `files._is_secret_name`. | `test_secret_files_cannot_be_read`, `test_secret_read_is_rejected_not_redacted`, `test_search_skips_secret_files`, `test_snapshot_excludes_secrets` |
| 10 | **Secret leakage through events** | The bus strips forbidden keys (`api_key`, `authorization`, `token`, `prompt`, `content`…) and truncates long values, at publish time rather than trusting callers. | `test_forbidden_payload_keys_are_stripped`, `test_long_payload_values_are_truncated` |
| 11 | **Secret leakage through config repr** | `AIConfig.__repr__` and `__str__` print `<set>`/`<unset>`, never the key. Debuggers and exception handlers print reprs. | `test_ai_config_never_reveals_the_key` |
| 12 | **Resource exhaustion — file size** | Both `max_write_size` and `max_file_size` enforced *before* the write, independently, so an oversized file is never briefly on disk. | `test_read_rejects_oversized_file`, `test_write_rejects_oversized_content`, `test_max_file_size_is_enforced_independently` |
| 13 | **Resource exhaustion — project growth (incl. races)** | `max_project_size` and `max_files` checked on every write, inside a per-workspace lock so concurrent writers cannot race past the limit. | `test_write_enforces_project_size_limit`, `test_concurrent_writes_respect_the_project_limit` |
| 14 | **Resource exhaustion — traversal depth** | `max_directory_depth` prunes `os.walk`; ignored trees skipped **at the project root only**, so nested `src/build/` is never mistaken for a generated tree. | `test_list_files_skips_node_modules`, `test_rollback_preserves_nested_source_that_looks_generated` |
| 14a | **Resource exhaustion — snapshots** (2A.1) | `max_snapshots_per_workspace` and `max_snapshot_storage` enforced before each snapshot. | `test_snapshot_count_quota_is_enforced`, `test_snapshot_storage_quota_is_enforced` |
| 15 | **Regex denial of service** | Search is plain substring, not regex. A model-supplied pattern is untrusted input and a pathological regex is a DoS. | `test_search_rejects_empty_query` |
| 16 | **Snapshot tampering / secret revival** | Snapshots live outside the agent-reachable tree and exclude secrets, so a rollback cannot resurrect a credential. | `test_snapshot_lives_outside_the_agent_reachable_project`, `test_snapshot_excludes_secrets` |
| 16a | **Partial-restore data loss** (2A.1) | Restore stages the new tree first (the fallible step), then swaps by directory rename. A failure during staging leaves the live project exactly as it was. Not syscall-atomic; see Known risks. | `test_failed_restore_leaves_the_project_intact` |
| 17 | **Rollback affecting another project** | The store is per workspace and only writes under its own `project/`, inside the workspace lock. | `test_rollback_only_touches_its_own_project`, `test_two_workspaces_are_independent` |
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

**Hardlink/reparse defence is at the Redstone FS layer only.** `read_file`,
`write_file`, `rename_file` and snapshots now refuse unsafe hardlinks and never
traverse reparse points (fixed in 2A.1). This protects Redstone's *own* file
operations. It does **not** constrain project code once a runtime can execute
it: running code can create hardlinks, junctions and streams at will. Those
require OS-level isolation (container/VM), which does not exist yet.

**Restore is failure-safe, not syscall-atomic.** A failure while staging the
new tree leaves the project untouched. A crash between the two final directory
renames could leave the project directory missing, recoverable from
`.redstone/restore/trash_*`. True atomicity is not achievable across a
multi-file tree on the supported filesystems.

**TOCTOU on path resolution.** `resolve()` checks containment and then returns a
path the caller opens. A link created in that window could in principle redirect
the open. The hardlink/reparse checks re-stat at operation time, which narrows
but does not close the window. Full mitigation needs `O_NOFOLLOW`-style
handle-based operations, not portable to Windows. The exposure requires an
attacker already able to write into the workspace concurrently — which, again,
only becomes real once project code executes, and then belongs to OS isolation.

**Secret detection is heuristic.** Filename and canonical-name matching catch
the common cases and Windows aliases. Content scanning is not yet implemented,
so a credential pasted into `src/config.ts` would not be excluded.

## AI credential threat model (Phase 2B)

The AI gateway forwards a provider key to an upstream API. The key — whether
server-configured or supplied per request (BYOK) — must reach only the provider
auth header and nothing else.

| # | Threat | Mitigation | Test |
|---|---|---|---|
| 25 | **Key in a URL / query string** | Gemini uses the `x-goog-api-key` header, OpenAI-compatible a Bearer header. The key is never interpolated into a URL. | `test_request_url_never_contains_the_key`, `test_key_travels_only_in_the_auth_header` |
| 26 | **Key in logs** | The gateway logs only `request_id/provider/model/status/duration/attempt/error_code`, and passes the live key through `redact()` as belt-and-braces. | `test_logs_never_carry_the_key` |
| 27 | **Key in an error / exception** | Every failure is a `RedstoneAIError` with a Redstone-authored message; upstream exception text is redacted before it is even kept for internal logging. | `test_error_never_carries_the_key`, `test_http_status_maps_to_normalised_code` |
| 28 | **Key in a response** | `AIResponse` has no credential field; `to_dict()` never includes one. | `test_response_object_never_carries_the_key` |
| 29 | **Key persisted (snapshot/cache/pickle)** | `Credential` refuses pickle/copy, masks repr/str, and there is no credential store. | `test_credential_cannot_be_pickled_or_copied`, `test_credential_repr_is_masked`, `test_no_credential_persists_after_the_call` |
| 30 | **SSRF via base URL** | `validate_base_url` rejects non-https, credentials-in-URL, and private/loopback/link-local/metadata hosts; BYOK adds a host allowlist. | `test_dangerous_base_urls_are_rejected`, `test_gateway_rejects_ssrf_base_url_for_byok` |
| 31 | **Auth header following a redirect** | Redirects are disabled on the provider request; a 3xx is never followed. | `test_redirects_are_not_followed_to_another_host` |
| 32 | **Malformed provider response crashes backend** | Bodies are parsed defensively; a missing candidate or bad JSON becomes `MALFORMED_RESPONSE`. | `test_malformed_json_is_normalised`, `test_missing_candidate_is_malformed` |
| 33 | **Infinite retries / quota burn** | Bounded attempts; auth and invalid-request never retried. | `test_retries_are_bounded`, `test_non_retryable_statuses_are_not_retried` |
| 34 | **Unbounded provider output** | Response read incrementally, aborted past `MAX_AI_RESPONSE_SIZE`. | `test_oversized_response_is_rejected` |
| 35 | **Unbounded request** | Prompt size checked before any call. | `test_oversized_request_is_rejected_before_any_call` |
| 36 | **Arbitrary provider module import** | Providers resolve from a fixed allowlist; unknown names are a normalised error. | `test_unknown_provider_is_a_safe_error` |

**AI credential known limits.** DNS rebinding is not fully closed: SSRF
validation resolves the host at configuration time, but the address used at
request time could differ. Redirects being disabled and the BYOK host allowlist
narrow this. Full closure needs pinning the resolved address into the request,
deferred until it matters (no user BYOK endpoint is exposed yet).

## Verifying the boundary

```bash
python -m pytest tests/redstone/test_paths.py -q      # 67 rejection + 15 positive path tests
python -m pytest tests/redstone/test_hardening.py -q   # 2A.1 vulnerability regressions
python -m pytest tests/redstone/test_ai_gateway.py -q  # AI gateway + credential safety
python -m pytest tests/redstone/ -q                   # full foundation suite

# The deterministic core must never execute anything or import a web framework:
grep -rnE "subprocess|shell=True|eval\(|exec\(|os\.system" src/redstone/
grep -rnE "^\s*(import|from)\s+(fastapi|requests)" src/redstone/
# httpx is used ONLY by ai/providers, and only lazily inside post_json:
grep -rn "import httpx" src/redstone/   # -> src/redstone/ai/providers/base.py
```

## Reporting

This is a student project under active development and is **not production
software**. Do not deploy it where untrusted users can reach it until the
runtime isolation described above actually exists.
