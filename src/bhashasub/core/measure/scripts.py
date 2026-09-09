"""Script data for Indic text measurement.

Standard library only. Tables here are facts about Unicode, not policy, so they
belong beside the measurement code rather than in a specification file.
"""

from __future__ import annotations

__all__ = ["VIRAMAS", "ZERO_WIDTH_JOINERS", "SCRIPT_RANGES", "dominant_script"]

# Virama (halant / pulli / chandrakkala) for each Brahmic script that BhashaSub
# measures. A virama suppresses a consonant's inherent vowel and joins it to the
# following consonant, producing one orthographic syllable from several code
# points.
VIRAMAS: frozenset[str] = frozenset(
    {
        "्",  # Devanagari
        "্",  # Bengali
        "੍",  # Gurmukhi
        "્",  # Gujarati
        "୍",  # Oriya
        "்",  # Tamil (pulli)
        "్",  # Telugu
        "್",  # Kannada
        "്",  # Malayalam (chandrakkala)
        "්",  # Sinhala
    }
)

# ZWNJ and ZWJ control whether a conjunct renders as a ligature. They carry no
# width of their own, so they never count as a unit.
ZERO_WIDTH_JOINERS: frozenset[str] = frozenset({"‌", "‍"})

# Inclusive code point ranges, used only to report which script a run of text is
# written in. Not used for counting.
SCRIPT_RANGES: tuple[tuple[str, int, int], ...] = (
    ("Devanagari", 0x0900, 0x097F),
    ("Bengali", 0x0980, 0x09FF),
    ("Gurmukhi", 0x0A00, 0x0A7F),
    ("Gujarati", 0x0A80, 0x0AFF),
    ("Oriya", 0x0B00, 0x0B7F),
    ("Tamil", 0x0B80, 0x0BFF),
    ("Telugu", 0x0C00, 0x0C7F),
    ("Kannada", 0x0C80, 0x0CFF),
    ("Malayalam", 0x0D00, 0x0D7F),
    ("Sinhala", 0x0D80, 0x0DFF),
    ("Latin", 0x0041, 0x024F),
)


def dominant_script(text: str) -> str | None:
    """Return the script most of the letters in `text` belong to.

    Returns None when the text contains no letters from a known range. Ties are
    broken by the order of SCRIPT_RANGES, which keeps the result deterministic.
    """
    counts: dict[str, int] = {}

    for character in text:
        if not character.isalpha():
            continue
        code = ord(character)
        for name, low, high in SCRIPT_RANGES:
            if low <= code <= high:
                counts[name] = counts.get(name, 0) + 1
                break

    if not counts:
        return None

    best = max(counts.values())
    for name, _, _ in SCRIPT_RANGES:
        if counts.get(name) == best:
            return name
    return None
