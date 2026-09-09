import pytest

from bhashasub.core.formats.detect import DecodeError, decode, detect_format

MALAYALAM = "നമസ്കാരം"
TAMIL = "வணக்கம்"
SAMPLE = f"1\n00:00:01,000 --> 00:00:03,000\n{MALAYALAM}\n"


# ------------------------------------------------------------------- encoding

def test_plain_utf8():
    result = decode(SAMPLE.encode("utf-8"))

    assert result.encoding == "utf-8"
    assert result.confident
    assert not result.had_bom
    assert MALAYALAM in result.text


def test_utf8_with_bom_is_stripped():
    result = decode(SAMPLE.encode("utf-8-sig"))

    assert result.encoding == "utf-8-sig"
    assert result.had_bom
    assert result.confident
    assert result.text.startswith("1\n")     # BOM must not survive
    assert "﻿" not in result.text


@pytest.mark.parametrize("encoding", ["utf-16"])
def test_utf16_with_bom(encoding):
    """Only the bare "utf-16" codec emits a BOM; the LE/BE codecs do not."""
    result = decode(SAMPLE.encode(encoding))

    assert result.encoding.startswith("utf-16")
    assert result.had_bom
    assert result.confident
    assert MALAYALAM in result.text
    assert "﻿" not in result.text


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_utf16_without_bom_is_detected(encoding):
    raw = SAMPLE.encode(encoding)          # no BOM for explicit LE/BE codecs

    result = decode(raw)

    assert result.encoding == encoding
    assert not result.had_bom
    assert MALAYALAM in result.text
    assert result.note and "without a byte-order mark" in result.note


def test_utf32_with_bom():
    result = decode(SAMPLE.encode("utf-32"))

    assert result.encoding.startswith("utf-32")
    assert result.had_bom
    assert MALAYALAM in result.text


def test_bomless_utf32_is_not_silently_mangled_into_utf8():
    """NUL is valid UTF-8, so a naive decoder corrupts BOM-less UTF-32 silently."""
    result = decode(SAMPLE.encode("utf-32-le"))

    assert result.is_ambiguous
    assert "NUL characters" in result.note


def test_legacy_codepage_is_decoded_but_flagged_as_ambiguous():
    """Single-byte encodings always "succeed", so they can never be trusted."""
    raw = "1\n00:00:01,000 --> 00:00:03,000\nCaf\xe9 na\xefve\n".encode("cp1252")

    result = decode(raw)

    assert result.encoding in ("cp1252", "latin-1")
    assert result.is_ambiguous
    assert not result.confident
    assert "cannot be verified" in result.note
    assert "Café" in result.text


def test_empty_input():
    result = decode(b"")

    assert result.text == ""
    assert result.confident


def test_newlines_are_normalised():
    result = decode(b"1\r\n00:00:01,000 --> 00:00:03,000\r\nHi\r\n")

    assert "\r" not in result.text
    assert result.text == "1\n00:00:01,000 --> 00:00:03,000\nHi\n"


def test_lone_carriage_returns_are_normalised():
    assert decode(b"a\rb").text == "a\nb"


def test_decode_rejects_non_bytes():
    with pytest.raises(TypeError):
        decode("already text")


def test_bom_that_lies_about_its_encoding_raises():
    # A UTF-32-LE BOM followed by bytes that cannot form UTF-32 code units.
    with pytest.raises(DecodeError):
        decode(b"\xff\xfe\x00\x00\x01")


def test_mixed_script_content_survives():
    text = f"1\n00:00:01,000 --> 00:00:03,000\n{MALAYALAM} {TAMIL} hello\n"

    result = decode(text.encode("utf-8"))

    assert MALAYALAM in result.text
    assert TAMIL in result.text


# --------------------------------------------------------------------- format

@pytest.mark.parametrize(
    "text,expected",
    [
        ("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHi\n", "vtt"),
        ("﻿WEBVTT\n\nx", "vtt"),
        ("1\n00:00:01,000 --> 00:00:03,000\nHi\n", "srt"),
        ("[Script Info]\nTitle: x\n", "ass"),
        ("[V4+ Styles]\n", "ass"),
        ("just some prose", None),
        ("", None),
        ("   \n\n", None),
    ],
)
def test_detect_format(text, expected):
    assert detect_format(text) == expected
