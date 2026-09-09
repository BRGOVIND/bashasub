# Architecture

## The shape of the system

```
bytes ──► decode ──► parse ──► Track ──► measure ──► rules ──► Findings ──► report
                                             │                     │
                                             └── Spec (YAML) ──────┘
```

Everything to the left of `Findings` is deterministic: same input plus same spec
plus same version produces byte-identical output. AI, when it arrives, attaches
only at the far right and only as suggestions a human approves.

## Layout

```
src/bhashasub/
├── core/                 stdlib + `regex` only
│   ├── model.py          TimeCode, SourceSpan, Cue, Track          ✅
│   ├── formats/
│   │   ├── detect.py     encoding and format detection             ✅
│   │   ├── srt.py        parse / serialize                         ✅
│   │   └── vtt.py                                                  ⏳
│   ├── measure/          codepoint | grapheme | akshara            ⏳
│   ├── spec.py           declarative specification loader          ⏳
│   ├── rules/            one rule per file, pure functions         ⏳
│   ├── finding.py        Finding, Severity, fingerprints           ⏳
│   ├── fixes/            deterministic, idempotent fixers          ⏳
│   ├── report/           human, JSON, SARIF                        ⏳
│   └── baseline.py       suppression of known findings             ⏳
├── cli/                                                            ⏳
├── api/                  thin FastAPI over core                    ⏳
└── ai/                   optional providers and review             ⏳

main.py, config.py, ...    the existing web service, still running
```

The root-level `main.py` service is deliberately untouched. It keeps
`POST /translate` working while the core is built underneath it; the API will be
migrated onto the core in a later phase rather than rewritten in place.

## Rules that constrain the design

**The core must never import a framework.** No FastAPI, no httpx, no pydantic.
This is enforced by dependency extras in `pyproject.toml` and checked directly:

```bash
grep -rE "^\s*(import|from)\s+(fastapi|httpx|pydantic|uvicorn|tenacity)" src/bhashasub/core/
```

**`regex`, not `re`.** Python's standard `re` module cannot match grapheme
clusters — `re.compile(r"\X")` raises `bad escape \X`. Script-aware measurement
depends on it, so `regex` is the single core dependency.

**Integer milliseconds everywhere.** Floating-point time makes equality
unreliable and leaks rounding noise into findings, which must be reproducible.
Frame conversion is explicit and requires an FPS, because specifications state
gaps in frames — Netflix requires a minimum of 2 frames, which is 80 ms at
25 fps but 84 ms at 23.976. Frame-to-millisecond conversion rounds *up*, since
frame counts in specs are minimums and rounding down would let a too-small gap
pass.

**Provenance is not identity.** `Cue.span` records where a cue came from for
SARIF reporting, but is excluded from equality. Two cues with the same timing
and text are the same cue regardless of which line they were read from. This is
what allows the round-trip law to hold.

## The round-trip law

The correct property is that parsing is a **retraction**:

```
parse(serialize(parse(s))) == parse(s)      for any input s
```

This holds universally, including for malformed input. The more obvious-looking
`parse(serialize(track)) == track` is **false in general**, because serialization
legitimately normalises — cue numbering, spacing and line endings are all
canonicalised on the way out. Testing the wrong law forces a serializer that
cannot normalise, which in turn produces subtitle files that diff badly in git.

Deterministic serialization is tested separately, because stable byte output is
what makes subtitle files reviewable in version control. That, rather than a
bespoke versioning system, is the intended answer to subtitle history.

## Error handling

The parser distinguishes problems with the *file* from problems with the
*subtitles*:

- **`ParseIssue`** — the file could not be read as intended: a block with no
  timing line, an unreadable timestamp. Recoverable issues are collected and
  parsing continues; a linter that refuses to open a malformed file is useless,
  because malformed files are the ones worth checking.
- **`Finding`** (later) — the file was read fine, but a subtitle violates a
  specification: too short, too fast, overlapping.

An inverted cue (`end` before `start`) is kept by the parser, not rejected. It is
a rule violation, not a parse failure.

## Security invariants

Established in `583224d` and not to be regressed:

- API keys travel in request headers, never in URLs, logs, or responses.
- Exception text never crosses the boundary to a client.
- Failures use appropriate non-2xx status codes.
- User content is not written to normal logs.
- `redact()` strips credential-shaped text from anything destined for a log.

These are covered by 31 regression tests in `tests/test_security.py`.
