"""SubRip (.srt) parser and serializer.

The parser is deliberately lenient. A conformance linter that refuses to open a
malformed file is useless, because malformed files are exactly the ones worth
checking. Recoverable problems are collected as ParseIssue records and parsing
continues; only genuinely unusable blocks are skipped.

Note the division of labour: the parser reports problems with the *file*
(unreadable timestamps, missing timing lines). It does not report problems with
the *subtitles* (a cue that is too short, two cues that overlap). Those are
rules, and they run later against the parsed track.

Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..model import Cue, SourceSpan, TimeCode, TimeCodeError, Track

__all__ = ["ParseIssue", "ParseResult", "parse", "serialize"]

# Split on "-->" with optional surrounding whitespace. Trailing content after
# the end timestamp (WebVTT-style cue settings, or stray text) is captured so it
# can be reported rather than silently dropped.
_TIMING_RE = re.compile(
    r"^\s*(?P<start>[0-9:,.]+)\s*-->\s*(?P<end>[0-9:,.]+)\s*(?P<trailing>.*?)\s*$"
)

_INDEX_RE = re.compile(r"^\s*(?P<index>\d+)\s*$")

_BOM = "﻿"


@dataclass(frozen=True, slots=True)
class ParseIssue:
    """A problem found while reading the file itself."""

    line: int
    message: str
    severity: str = "error"


@dataclass(frozen=True, slots=True)
class ParseResult:
    track: Track
    issues: tuple[ParseIssue, ...] = ()

    @property
    def ok(self) -> bool:
        """True when nothing was lost. Warnings are tolerable; errors are not."""
        return not any(issue.severity == "error" for issue in self.issues)


def _split_blocks(lines: list[str]) -> list[tuple[int, list[str]]]:
    """Group lines into cue blocks separated by blank lines.

    Returns (1-based line number of the block's first line, block lines).
    """
    blocks: list[tuple[int, list[str]]] = []
    current: list[str] = []
    start_line = 0

    for offset, line in enumerate(lines, start=1):
        if line.strip():
            if not current:
                start_line = offset
            current.append(line)
        elif current:
            blocks.append((start_line, current))
            current = []

    if current:
        blocks.append((start_line, current))

    return blocks


def parse(
    text: str,
    *,
    fps: float | None = None,
    language: str | None = None,
    encoding: str | None = None,
) -> ParseResult:
    """Parse SRT text into a Track.

    `text` is expected to have normalised newlines; core.formats.detect.decode
    guarantees that.
    """
    source = (text or "").lstrip(_BOM)
    lines = source.split("\n")

    cues: list[Cue] = []
    issues: list[ParseIssue] = []
    next_fallback_index = 1

    for block_start, block in _split_blocks(lines):
        cursor = 0
        index: int | None = None

        # An index line is optional in practice. Only treat a leading bare
        # integer as an index when a timing line follows it, so that a cue whose
        # first line of dialogue is a number is not misread.
        index_match = _INDEX_RE.match(block[cursor])
        if (
            index_match
            and cursor + 1 < len(block)
            and "-->" in block[cursor + 1]
        ):
            index = int(index_match.group("index"))
            cursor += 1

        if cursor >= len(block) or "-->" not in block[cursor]:
            issues.append(
                ParseIssue(
                    line=block_start,
                    message="block has no timing line and was skipped",
                    severity="error",
                )
            )
            continue

        timing_line_number = block_start + cursor
        timing_match = _TIMING_RE.match(block[cursor])
        if not timing_match:
            issues.append(
                ParseIssue(
                    line=timing_line_number,
                    message=f"malformed timing line: {block[cursor].strip()!r}",
                    severity="error",
                )
            )
            continue

        try:
            start = TimeCode.parse(timing_match.group("start"))
            end = TimeCode.parse(timing_match.group("end"))
        except TimeCodeError as exc:
            issues.append(
                ParseIssue(
                    line=timing_line_number,
                    message=f"unreadable timestamp: {exc}",
                    severity="error",
                )
            )
            continue

        trailing = timing_match.group("trailing")
        if trailing:
            issues.append(
                ParseIssue(
                    line=timing_line_number,
                    message=f"ignored trailing text on timing line: {trailing!r}",
                    severity="warning",
                )
            )

        cursor += 1
        text_lines = tuple(block[cursor:])

        if index is None:
            index = next_fallback_index
            issues.append(
                ParseIssue(
                    line=block_start,
                    message=f"cue has no index; numbered it {index}",
                    severity="warning",
                )
            )

        next_fallback_index = index + 1

        cues.append(
            Cue(
                index=index,
                start=start,
                end=end,
                lines=text_lines,
                span=SourceSpan(
                    start_line=block_start,
                    end_line=block_start + len(block) - 1,
                ),
            )
        )

    track = Track(
        cues=tuple(cues),
        format="srt",
        language=language,
        fps=fps,
        encoding=encoding,
    )

    return ParseResult(track=track, issues=tuple(issues))


def serialize(track: Track, *, renumber: bool = False) -> str:
    """Render a Track as canonical SRT text.

    Canonical means: cue index, timing line, text lines, then exactly one blank
    line between blocks and a single trailing newline. Deterministic output is
    what makes subtitle files diff cleanly in git, so this function must never
    depend on anything but the track's contents.

    Cue indices are preserved by default. Pass renumber=True to rewrite them as
    1..N, which is a change to the user's data and therefore opt-in.
    """
    parts: list[str] = []

    for position, cue in enumerate(track.cues, start=1):
        index = position if renumber else cue.index
        body = "\n".join(cue.lines)
        block = f"{index}\n{cue.start.to_srt()} --> {cue.end.to_srt()}\n{body}"
        parts.append(block.rstrip("\n"))

    if not parts:
        return ""

    return "\n\n".join(parts) + "\n"
