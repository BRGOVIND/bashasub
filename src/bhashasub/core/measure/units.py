"""Counting units for subtitle text.

Subtitle specifications state limits in "characters" — 42 per line, 20 per
second — without ever defining what a character is. For Latin text the question
does not arise. For Indic scripts it decides the outcome.

The measurement study in docs/measurement-study.md counted 10,510 real cues
across seven Indic languages and found that counting code points overstates
length by 50-84%, and that 18.1% of cues change their pass/fail verdict at
20 CPS purely because of the unit chosen. Malayalam fails 29.92% of cues when
counted as code points against 0.65% when counted as graphemes.

Hence: **GRAPHEME is the default**, CODEPOINT is kept for comparison and for
reproducing what other tools report, and AKSHARA is experimental.

Depends on `regex` rather than the standard `re` because only `regex` implements
`\\X`, the extended grapheme cluster. `re.compile(r"\\X")` raises `bad escape`.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum

import regex

from .scripts import VIRAMAS, ZERO_WIDTH_JOINERS, dominant_script

__all__ = [
    "Unit",
    "Measurement",
    "segment",
    "count",
    "measure",
    "reading_speed",
    "DEFAULT_UNIT",
]

_GRAPHEME = regex.compile(r"\X")


class Unit(str, Enum):
    """A way of counting the length of subtitle text."""

    CODEPOINT = "codepoint"
    GRAPHEME = "grapheme"
    AKSHARA = "akshara"

    @property
    def is_experimental(self) -> bool:
        return self is Unit.AKSHARA


DEFAULT_UNIT = Unit.GRAPHEME


def _codepoints(text: str) -> tuple[str, ...]:
    return tuple(text)


def _graphemes(text: str) -> tuple[str, ...]:
    """Extended grapheme clusters per UAX #29.

    Since Unicode 15.1 this rule (GB9c) already keeps virama-joined conjuncts
    together for most Brahmic scripts, so `क्ष` and `ക്ഷ` each count as one.
    """
    return tuple(_GRAPHEME.findall(text))


def _aksharas(text: str) -> tuple[str, ...]:
    """Orthographic syllables. Experimental.

    Grapheme clusters, with any cluster ending in a virama or zero-width joiner
    merged into the cluster that follows it.

    The measurement study found this adds little over graphemes: the two units
    are *identical* for Hindi, Marathi and Bengali, and differ for at most 3.2%
    of verdicts in Kannada and Tamil. It is provided for research and for the
    scripts where it does differ, not as a recommended default.

    Known limitation: in Tamil a consonant plus pulli (`க்`) is already a
    complete written unit, so merging it into the next cluster over-merges.
    Tamil akshara counts should be treated as a lower bound.
    """
    merged: list[str] = []
    carry = ""

    for cluster in _graphemes(text):
        candidate = carry + cluster
        last = cluster[-1] if cluster else ""
        if last in VIRAMAS or last in ZERO_WIDTH_JOINERS:
            carry = candidate
            continue
        merged.append(candidate)
        carry = ""

    if carry:
        # Trailing virama with nothing to join to; keep it rather than drop it.
        merged.append(carry)

    return tuple(merged)


_SEGMENTERS = {
    Unit.CODEPOINT: _codepoints,
    Unit.GRAPHEME: _graphemes,
    Unit.AKSHARA: _aksharas,
}


def segment(text: str, unit: Unit = DEFAULT_UNIT, *, normalize: str | None = None) -> tuple[str, ...]:
    """Split `text` into units.

    `normalize` optionally applies a Unicode normalisation form ("NFC", "NFD",
    "NFKC", "NFKD") first. It defaults to None: measurement reports what is
    actually in the file, because a linter's job is to describe the bytes it was
    given, not a tidied version of them. Pass "NFC" when comparing text from
    different sources that may be encoded differently.
    """
    if not isinstance(text, str):
        raise TypeError(f"expected str, got {type(text).__name__}")

    unit = Unit(unit)

    if normalize:
        text = unicodedata.normalize(normalize, text)

    return _SEGMENTERS[unit](text)


def count(text: str, unit: Unit = DEFAULT_UNIT, *, normalize: str | None = None) -> int:
    """Length of `text` in the given unit."""
    return len(segment(text, unit, normalize=normalize))


@dataclass(frozen=True, slots=True)
class Measurement:
    """The length of one piece of text under every unit.

    All three are always reported. A finding that says only "31 CPS" is
    unactionable when another tool says 17; one that reports both, and names the
    unit it judged against, is not.
    """

    text: str
    codepoints: int
    graphemes: int
    aksharas: int
    script: str | None = None

    def by(self, unit: Unit) -> int:
        unit = Unit(unit)
        if unit is Unit.CODEPOINT:
            return self.codepoints
        if unit is Unit.GRAPHEME:
            return self.graphemes
        return self.aksharas

    @property
    def codepoints_per_grapheme(self) -> float:
        """How much a code-point count overstates this text. 1.0 means not at all."""
        if not self.graphemes:
            return 0.0
        return self.codepoints / self.graphemes


def measure(text: str, *, normalize: str | None = None) -> Measurement:
    """Measure `text` under all three units in one pass."""
    if not isinstance(text, str):
        raise TypeError(f"expected str, got {type(text).__name__}")

    if normalize:
        text = unicodedata.normalize(normalize, text)

    return Measurement(
        text=text,
        codepoints=len(text),
        graphemes=len(_graphemes(text)),
        aksharas=len(_aksharas(text)),
        script=dominant_script(text),
    )


def reading_speed(units: int, duration_ms: int) -> float:
    """Units per second.

    Pure arithmetic, deliberately unopinionated: it does not know what a
    threshold is or whether a value passes. Judgement belongs to the rule
    engine, which does not exist yet.

    Returns 0.0 for a non-positive duration rather than raising, so a malformed
    cue can still be measured and reported rather than aborting a whole file.
    """
    if duration_ms <= 0:
        return 0.0
    return units / (duration_ms / 1000.0)
