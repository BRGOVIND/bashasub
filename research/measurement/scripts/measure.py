"""Measure every cue in the corpus under three counting units.

Deliberately lives in research/ rather than in bhashasub.core. The point of the
study is to decide whether script-aware measurement earns a place in the core;
implementing it there first would prejudge that.

Cues are parsed with BhashaSub's own SRT parser, so the study exercises the
code the product actually ships.

    python research/measurement/scripts/measure.py
"""

from __future__ import annotations

import csv
import json
import sys
import unicodedata
from pathlib import Path

import regex

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from bhashasub.core.formats.detect import decode  # noqa: E402
from bhashasub.core.formats.srt import parse  # noqa: E402

BASE = Path(__file__).resolve().parents[1]
CORPUS = BASE / "corpus"
RESULTS = BASE / "results"

# Virama (halant) per Brahmic script. A virama suppresses the inherent vowel and
# joins the surrounding consonants into one orthographic syllable.
VIRAMAS = {
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

ZERO_WIDTH = {"‌", "‍"}  # ZWNJ, ZWJ

GRAPHEME = regex.compile(r"\X")


def count_codepoints(text: str) -> int:
    """Unit A. What ``len(text)`` returns, and what most tooling counts."""
    return len(text)


def graphemes(text: str) -> list[str]:
    """Unit B. Extended grapheme clusters per UAX #29, via regex's \\X."""
    return GRAPHEME.findall(text)


def aksharas(text: str) -> list[str]:
    """Unit C. Orthographic syllables.

    Built on grapheme clusters, then merging any cluster that ends in a virama
    (or a zero-width joiner) into the cluster that follows it. This is the
    linguistic definition of an akshara: a consonant cluster plus its vowel,
    written and read as one unit.

    Limitations, stated plainly:
      * Only handles virama-joined conjuncts. It does not model script-specific
        exceptions such as Malayalam chillu letters, which are historically
        conjunct-derived but are encoded atomically.
      * Tamil is a genuine edge case. A consonant plus pulli is a complete
        written unit in Tamil, so merging it into the next cluster may
        over-merge. Results are reported per language so this is visible.
      * Non-Indic runs (Latin, digits, punctuation, spaces) pass through as one
        unit each, matching how grapheme clusters treat them.
    """
    clusters = graphemes(text)
    merged: list[str] = []
    carry = ""

    for cluster in clusters:
        candidate = carry + cluster
        if cluster and (cluster[-1] in VIRAMAS or cluster[-1] in ZERO_WIDTH):
            carry = candidate
            continue
        merged.append(candidate)
        carry = ""

    if carry:
        merged.append(carry)

    return merged


def dominant_script(text: str) -> str:
    counts: dict[str, int] = {}
    for character in text:
        if not character.isalpha():
            continue
        try:
            name = unicodedata.name(character).split(" ")[0]
        except ValueError:
            continue
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return "NONE"
    return max(counts, key=counts.get)


def cps(count: int, duration_ms: int) -> float | str:
    if duration_ms <= 0:
        return ""
    return round(count / (duration_ms / 1000.0), 4)


def measure_cue(cue, language: str, source_file: str) -> dict:
    text = cue.text
    flat = text.replace("\n", " ")
    duration = cue.duration_ms

    cp = count_codepoints(flat)
    gr = len(graphemes(flat))
    ak = len(aksharas(flat))

    line_cp = [count_codepoints(line) for line in cue.lines] or [0]
    line_gr = [len(graphemes(line)) for line in cue.lines] or [0]
    line_ak = [len(aksharas(line)) for line in cue.lines] or [0]

    letters = sum(1 for c in flat if c.isalpha())
    digits = sum(1 for c in flat if c.isdigit())
    spaces = sum(1 for c in flat if c.isspace())
    punctuation = sum(1 for c in flat if not c.isalnum() and not c.isspace())

    return {
        "file": source_file,
        "language": language,
        "script": dominant_script(flat),
        "cue_index": cue.index,
        "duration_ms": duration,
        "n_lines": len(cue.lines),
        "codepoints": cp,
        "graphemes": gr,
        "aksharas": ak,
        "cps_codepoint": cps(cp, duration),
        "cps_grapheme": cps(gr, duration),
        "cps_akshara": cps(ak, duration),
        "max_line_codepoints": max(line_cp),
        "max_line_graphemes": max(line_gr),
        "max_line_aksharas": max(line_ak),
        "letters": letters,
        "digits": digits,
        "spaces": spaces,
        "punctuation": punctuation,
        "text": flat,
    }


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((CORPUS / "MANIFEST.json").read_text(encoding="utf-8"))

    rows: list[dict] = []
    skipped: list[str] = []

    for entry in manifest:
        path = CORPUS / entry["file"]
        if not path.exists():
            skipped.append(entry["file"])
            continue

        result = decode(path.read_bytes())
        parsed = parse(result.text, language=entry["language"])

        for cue in parsed.track:
            # Empty cues carry no text to measure and would skew every ratio.
            if cue.is_empty or cue.duration_ms <= 0:
                continue
            rows.append(measure_cue(cue, entry["language"], entry["file"]))

    output = RESULTS / "measurements.csv"
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    by_language: dict[str, int] = {}
    for row in rows:
        by_language[row["language"]] = by_language.get(row["language"], 0) + 1

    print(f"measured {len(rows)} cues from {len(manifest) - len(skipped)} files")
    print("cues per language:", dict(sorted(by_language.items())))
    print("wrote", output)


if __name__ == "__main__":
    main()
