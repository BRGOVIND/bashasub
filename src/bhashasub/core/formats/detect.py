"""Encoding and format detection for subtitle files.

Real-world subtitle files are rarely clean UTF-8. They arrive as UTF-8 with a
BOM, UTF-16 from Windows tooling, or legacy single-byte codepages from the
fansub era. Mis-detecting the encoding silently corrupts every non-ASCII
character, which for Indic scripts means the entire file.

The rule here is: never guess silently. When a decode is not certain, say so
and let the caller decide.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DecodeResult", "DecodeError", "decode", "detect_format"]


class DecodeError(Exception):
    """The bytes could not be decoded by any supported encoding."""


# Byte-order marks, longest first so UTF-32 is not shadowed by UTF-16.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\xff\xfe", "utf-16-le"),
)

# Tried in order when the bytes are not valid UTF-8. cp1252 first because it is
# a superset of latin-1 for the printable range and far more common in files
# produced on Windows.
_LEGACY_FALLBACKS: tuple[str, ...] = ("cp1252", "latin-1")


@dataclass(frozen=True, slots=True)
class DecodeResult:
    """The outcome of decoding subtitle bytes."""

    text: str
    encoding: str
    had_bom: bool = False
    confident: bool = True
    note: str | None = None

    @property
    def is_ambiguous(self) -> bool:
        return not self.confident


def _looks_like_bomless_utf16(data: bytes) -> str | None:
    """Detect BOM-less UTF-16 by looking at the distribution of NUL bytes.

    Text that is mostly ASCII encoded as UTF-16 has a NUL in every other byte.
    Which half carries the NULs tells us the byte order.
    """
    sample = data[:4096]
    if len(sample) < 4:
        return None

    even_nulls = sample[0::2].count(0)
    odd_nulls = sample[1::2].count(0)
    half = len(sample) // 2
    if half == 0:
        return None

    # A high proportion of NULs in exactly one half is the signature.
    if odd_nulls / half > 0.6 and even_nulls / half < 0.1:
        return "utf-16-le"
    if even_nulls / half > 0.6 and odd_nulls / half < 0.1:
        return "utf-16-be"
    return None


def _normalise_newlines(text: str) -> str:
    """Collapse CRLF and lone CR to LF.

    Line endings are a serialisation concern, not a content one. Normalising at
    the boundary keeps every downstream rule and line-number calculation from
    having to care.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def decode(data: bytes) -> DecodeResult:
    """Decode subtitle bytes to text, reporting how confident the result is.

    Detection order:
      1. An explicit byte-order mark. Unambiguous.
      2. BOM-less UTF-16, detected from NUL-byte distribution.
      3. Strict UTF-8. Self-validating, so success is strong evidence.
      4. Legacy single-byte codepages. Always reported as not confident,
         because these encodings cannot fail and therefore cannot be verified.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"expected bytes, got {type(data).__name__}")

    data = bytes(data)

    if not data:
        return DecodeResult(text="", encoding="utf-8", confident=True)

    for bom, encoding in _BOMS:
        if data.startswith(bom):
            try:
                text = data.decode(encoding)
            except UnicodeDecodeError as exc:
                raise DecodeError(
                    f"file begins with a {encoding} byte-order mark but does not "
                    f"decode as {encoding}"
                ) from exc
            # utf-8-sig strips the BOM; the utf-16/32 codecs do not always.
            return DecodeResult(
                text=_normalise_newlines(text.lstrip("﻿")),
                encoding=encoding,
                had_bom=True,
                confident=True,
            )

    bomless = _looks_like_bomless_utf16(data)
    if bomless:
        try:
            text = data.decode(bomless)
        except UnicodeDecodeError:
            pass
        else:
            return DecodeResult(
                text=_normalise_newlines(text.lstrip("﻿")),
                encoding=bomless,
                had_bom=False,
                confident=True,
                note="detected UTF-16 without a byte-order mark",
            )

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        # NUL is a valid UTF-8 code point, so BOM-less UTF-16/UTF-32 can decode
        # "successfully" into interleaved NULs and garbage. Real subtitle text
        # never contains NUL, so treat it as evidence the guess is wrong.
        if "\x00" in text:
            return DecodeResult(
                text=_normalise_newlines(text),
                encoding="utf-8",
                confident=False,
                note=(
                    "decoded as UTF-8 but the result contains NUL characters, "
                    "which usually means the file is really UTF-16 or UTF-32 "
                    "without a byte-order mark. Re-save the file as UTF-8."
                ),
            )
        return DecodeResult(
            text=_normalise_newlines(text), encoding="utf-8", confident=True
        )

    for encoding in _LEGACY_FALLBACKS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return DecodeResult(
            text=_normalise_newlines(text),
            encoding=encoding,
            confident=False,
            note=(
                f"not valid UTF-8; decoded as {encoding}. Single-byte encodings "
                "cannot be verified, so non-ASCII characters may be wrong. "
                "Re-save the file as UTF-8 to remove this ambiguity."
            ),
        )

    raise DecodeError("could not decode the file with any supported encoding")


def detect_format(text: str) -> str | None:
    """Identify the subtitle format from its content.

    Returns "vtt", "ass", "srt", or None when nothing matches.
    """
    stripped = (text or "").lstrip("﻿").lstrip()
    if not stripped:
        return None

    if stripped.startswith("WEBVTT"):
        return "vtt"

    lowered = stripped[:2048].lower()
    if "[script info]" in lowered or "[v4+ styles]" in lowered:
        return "ass"

    if "-->" in stripped:
        # WebVTT uses "." for fractional seconds and SRT uses ",", but files in
        # the wild mix them. The WEBVTT header above is the reliable signal, so
        # anything else carrying an arrow is treated as SRT.
        return "srt"

    return None
