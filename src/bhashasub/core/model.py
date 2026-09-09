"""Core subtitle domain model.

Standard library only: no third-party imports, no I/O, no network. Everything
here is immutable so that fixes can be checked for idempotence and tracks can
be compared directly in tests.

Time is stored as integer milliseconds throughout. Floating point milliseconds
would make equality comparisons unreliable and would leak rounding noise into
findings, which must be reproducible byte-for-byte.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Iterator

__all__ = [
    "SubtitleError",
    "TimeCodeError",
    "TimeCode",
    "SourceSpan",
    "Cue",
    "Track",
    "frames_to_ms",
    "ms_to_frames",
]


class SubtitleError(Exception):
    """Base class for domain-level subtitle errors."""


class TimeCodeError(SubtitleError):
    """A timecode was malformed or out of range."""


_MS_PER_SECOND = 1000
_MS_PER_MINUTE = 60 * _MS_PER_SECOND
_MS_PER_HOUR = 60 * _MS_PER_MINUTE


def frames_to_ms(frames: int, fps: float) -> int:
    """Convert a frame count to milliseconds, rounding up.

    Rounding up is deliberate. Frame counts appear in specifications as
    *minimum* separations (for example "at least 2 frames between subtitles").
    Rounding down would let a gap that is fractionally too small pass.
    """
    if fps <= 0:
        raise TimeCodeError(f"fps must be positive, got {fps!r}")
    if frames < 0:
        raise TimeCodeError(f"frames must not be negative, got {frames!r}")

    return math.ceil(frames * _MS_PER_SECOND / fps)


def ms_to_frames(ms: int, fps: float) -> int:
    """Convert milliseconds to a whole number of frames, rounding down."""
    if fps <= 0:
        raise TimeCodeError(f"fps must be positive, got {fps!r}")

    return math.floor(ms * fps / _MS_PER_SECOND)


@dataclass(frozen=True, slots=True, order=True)
class TimeCode:
    """A point on the subtitle timeline, in whole milliseconds."""

    ms: int

    def __post_init__(self) -> None:
        if isinstance(self.ms, bool) or not isinstance(self.ms, int):
            raise TimeCodeError(f"TimeCode.ms must be an int, got {type(self.ms).__name__}")
        if self.ms < 0:
            raise TimeCodeError(f"TimeCode.ms must not be negative, got {self.ms}")

    # ---------------------------------------------------------------- parsing

    @classmethod
    def parse(cls, text: str) -> TimeCode:
        """Parse ``HH:MM:SS,mmm`` or ``HH:MM:SS.mmm``.

        The hours field may be omitted (WebVTT permits ``MM:SS.mmm``) and may
        exceed two digits for very long programmes. Both ``,`` and ``.`` are
        accepted as the decimal separator so the same routine serves SRT and
        WebVTT.
        """
        raw = (text or "").strip()
        if not raw:
            raise TimeCodeError("empty timecode")

        body, sep, frac = raw.replace(",", ".").rpartition(".")
        if not sep:
            body, frac = raw, "0"

        parts = body.split(":")
        if len(parts) == 2:
            parts = ["0", *parts]
        if len(parts) != 3:
            raise TimeCodeError(f"malformed timecode: {text!r}")

        try:
            hours, minutes, seconds = (int(part) for part in parts)
            # Pad or trim to exactly three digits so ".5" means 500ms.
            milliseconds = int(f"{frac:0<3.3}")
        except ValueError:
            raise TimeCodeError(f"malformed timecode: {text!r}") from None

        if minutes > 59 or seconds > 59:
            raise TimeCodeError(f"minutes and seconds must be 0-59: {text!r}")
        if hours < 0 or minutes < 0 or seconds < 0:
            raise TimeCodeError(f"negative component in timecode: {text!r}")

        return cls(
            hours * _MS_PER_HOUR
            + minutes * _MS_PER_MINUTE
            + seconds * _MS_PER_SECOND
            + milliseconds
        )

    @classmethod
    def from_frames(cls, frames: int, fps: float) -> TimeCode:
        return cls(frames_to_ms(frames, fps))

    # -------------------------------------------------------------- rendering

    @property
    def parts(self) -> tuple[int, int, int, int]:
        """(hours, minutes, seconds, milliseconds)."""
        remainder, milliseconds = divmod(self.ms, _MS_PER_SECOND)
        remainder, seconds = divmod(remainder, 60)
        hours, minutes = divmod(remainder, 60)
        return hours, minutes, seconds, milliseconds

    def to_srt(self) -> str:
        hours, minutes, seconds, milliseconds = self.parts
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

    def to_vtt(self) -> str:
        hours, minutes, seconds, milliseconds = self.parts
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"

    def to_frames(self, fps: float) -> int:
        return ms_to_frames(self.ms, fps)

    # ------------------------------------------------------------- arithmetic

    def __add__(self, other: int) -> TimeCode:
        return TimeCode(self.ms + int(other))

    def __sub__(self, other: TimeCode | int) -> TimeCode | int:
        """``TimeCode - TimeCode`` is a duration in ms; ``TimeCode - int`` shifts."""
        if isinstance(other, TimeCode):
            return self.ms - other.ms
        return TimeCode(self.ms - int(other))

    def __str__(self) -> str:
        return self.to_srt()


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """Where a cue came from in its source file.

    Line numbers are 1-based and inclusive. SARIF reporting requires these, so
    they are captured during parsing rather than reconstructed later.
    """

    start_line: int
    end_line: int
    byte_offset: int | None = None

    def __post_init__(self) -> None:
        if self.start_line < 1:
            raise SubtitleError(f"start_line is 1-based, got {self.start_line}")
        if self.end_line < self.start_line:
            raise SubtitleError(
                f"end_line {self.end_line} precedes start_line {self.start_line}"
            )


@dataclass(frozen=True, slots=True)
class Cue:
    """A single subtitle event."""

    index: int
    start: TimeCode
    end: TimeCode
    lines: tuple[str, ...] = ()
    # Provenance, not content: two cues with the same timing and text are equal
    # regardless of where they were read from. Excluding it from comparison is
    # what lets parse(serialize(parse(s))) == parse(s) hold, since re-parsing
    # canonical output legitimately yields different line numbers.
    span: SourceSpan | None = field(default=None, compare=False)

    @property
    def duration_ms(self) -> int:
        """Duration in milliseconds. Negative if the cue is inverted."""
        return self.end.ms - self.start.ms

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def is_empty(self) -> bool:
        return not any(line.strip() for line in self.lines)

    @property
    def has_valid_timing(self) -> bool:
        return self.end.ms > self.start.ms

    def gap_to(self, following: Cue) -> int:
        """Milliseconds between this cue's end and the next cue's start.

        Negative when the two cues overlap.
        """
        return following.start.ms - self.end.ms

    def overlaps(self, other: Cue) -> bool:
        return self.start.ms < other.end.ms and other.start.ms < self.end.ms

    def replace(self, **changes) -> Cue:
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class Track:
    """An ordered collection of cues plus the context needed to check them."""

    cues: tuple[Cue, ...] = ()
    format: str = "srt"
    language: str | None = None
    fps: float | None = None
    encoding: str | None = None
    meta: dict[str, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.cues)

    def __iter__(self) -> Iterator[Cue]:
        return iter(self.cues)

    def __getitem__(self, position: int) -> Cue:
        return self.cues[position]

    @property
    def duration_ms(self) -> int:
        """End of the last cue, or 0 for an empty track."""
        return max((cue.end.ms for cue in self.cues), default=0)

    @property
    def is_ordered(self) -> bool:
        """True when cues are sorted by start time."""
        starts = [cue.start.ms for cue in self.cues]
        return all(a <= b for a, b in zip(starts, starts[1:]))

    def pairs(self) -> Iterator[tuple[Cue, Cue]]:
        """Yield consecutive cue pairs, for gap and overlap checks."""
        return zip(self.cues, self.cues[1:])

    def with_cues(self, cues: tuple[Cue, ...]) -> Track:
        return replace(self, cues=tuple(cues))
