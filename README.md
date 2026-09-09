# BhashaSub

**A subtitle conformance linter.**

BhashaSub parses subtitle files, measures them correctly for the script they are
written in, checks them against declarative specifications, and explains exactly
why a file would pass or fail delivery QC.

Reading speed is one of the most common reasons subtitle files are rejected by
platform QC. The published thresholds — Netflix allows 42 characters per line
and 22 characters per second for Tamil, against 20 CPS for English — do not
define what a "character" is in an Indic script. Depending on whether you count
Unicode code points, grapheme clusters, or orthographic syllables, the same line
measures very differently. BhashaSub is being built to measure it properly and
to say which unit a given threshold assumes.

> **Status: early development.** The deterministic core is being built first.
> The list below describes what is actually implemented today, not what is
> planned. See [ARCHITECTURE.md](ARCHITECTURE.md) for where this is going.

## What works today

| Component | Status |
|---|---|
| Domain model (`TimeCode`, `Cue`, `Track`, `SourceSpan`) | ✅ |
| Encoding detection (UTF-8, BOM, UTF-16/32, legacy codepages) | ✅ |
| SRT parser and serializer, with source line spans | ✅ |
| Property-based round-trip and determinism tests | ✅ |
| Translation demo endpoint (`POST /translate`, Gemini) | ✅ |
| Script-aware measurement | ⏳ not yet |
| Rule engine, specifications, findings | ⏳ not yet |
| CLI, SARIF output, fixes | ⏳ not yet |

## Design principles

- **Deterministic engineering first, AI second.** Timing, readability and
  formatting checks are arithmetic. They need no API key, no network and no GPU,
  and they produce identical results on every run.
- **The core has one dependency.** `bhashasub.core` imports only the standard
  library plus `regex`, which is required because Python's `re` cannot match
  grapheme clusters (`\X`). Web and AI functionality are optional extras.
- **Findings over scores.** A single quality number is not useful until there is
  benchmark data to calibrate it against, so there isn't one yet.
- **Never modify a subtitle silently.** Fixes will always be explicit and
  reviewable as a diff.

## Install

```bash
pip install -e .           # core + tests
pip install -e ".[api]"    # adds the FastAPI web service
pip install -e ".[ai]"     # adds AI providers
```

## Use the core

```python
from bhashasub.core.formats.detect import decode
from bhashasub.core.formats.srt import parse, serialize

result = decode(open("episode.srt", "rb").read())
if result.is_ambiguous:
    print("warning:", result.note)

track = parse(result.text, fps=25.0, language="ml").track
print(len(track), "cues,", track.duration_ms, "ms")
```

## Run the web service

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Set `GEMINI_API_KEY` in the environment or a `.env` file (see `.env.example`).
The key is sent only as the `x-goog-api-key` request header, never in a URL.

## Tests

```bash
python -m pytest tests/ -q
```

## License

[Apache-2.0](LICENSE).
