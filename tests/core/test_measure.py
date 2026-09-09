import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bhashasub.core.measure import (
    DEFAULT_UNIT,
    Measurement,
    Unit,
    count,
    dominant_script,
    measure,
    reading_speed,
    segment,
)

# Real words, with the counts worked out by hand from the Unicode sequences.
MALAYALAM_HELLO = "നമസ്കാരം"      # 8 code points, 4 grapheme clusters
TAMIL_HELLO = "வணக்கம்"           # 7 code points, 5 clusters
HINDI_HELLO = "नमस्ते"             # 6 code points, 3 clusters
DEVA_KSHA = "क्ष"                  # ka + virama + ssa
MLYM_KSHA = "ക്ഷ"
TAMIL_KSHA = "க்ஷ"


# ------------------------------------------------------------------- defaults

def test_grapheme_is_the_default_unit():
    """The measurement study's central conclusion, encoded as a default."""
    assert DEFAULT_UNIT is Unit.GRAPHEME
    assert count(MALAYALAM_HELLO) == count(MALAYALAM_HELLO, Unit.GRAPHEME)


def test_only_akshara_is_experimental():
    assert Unit.AKSHARA.is_experimental
    assert not Unit.GRAPHEME.is_experimental
    assert not Unit.CODEPOINT.is_experimental


def test_unit_accepts_its_string_value():
    assert count("abc", "codepoint") == 3
    assert Unit("grapheme") is Unit.GRAPHEME


def test_unknown_unit_is_rejected():
    with pytest.raises(ValueError):
        count("abc", "syllable")


# --------------------------------------------------------------------- counts

@pytest.mark.parametrize(
    "text,codepoints,graphemes",
    [
        ("", 0, 0),
        ("abc", 3, 3),
        ("hello world", 11, 11),
        ("1234", 4, 4),
        (MALAYALAM_HELLO, 8, 4),
        (TAMIL_HELLO, 7, 5),
        (HINDI_HELLO, 6, 3),
    ],
)
def test_codepoint_and_grapheme_counts(text, codepoints, graphemes):
    assert count(text, Unit.CODEPOINT) == codepoints
    assert count(text, Unit.GRAPHEME) == graphemes


@pytest.mark.parametrize("text", [DEVA_KSHA, MLYM_KSHA])
def test_gb9c_keeps_conjuncts_together(text):
    """Unicode 15.1's GB9c already clusters virama conjuncts for these scripts.

    This is why the project does not need a bespoke akshara unit as its
    headline feature: graphemes already do the work here.
    """
    assert count(text, Unit.CODEPOINT) == 3
    assert count(text, Unit.GRAPHEME) == 1
    assert count(text, Unit.AKSHARA) == 1


def test_tamil_splits_at_the_pulli_but_akshara_merges_it():
    """Tamil is the case where the two units genuinely differ."""
    assert count(TAMIL_KSHA, Unit.CODEPOINT) == 3
    assert count(TAMIL_KSHA, Unit.GRAPHEME) == 2
    assert count(TAMIL_KSHA, Unit.AKSHARA) == 1


def test_ascii_is_identical_under_every_unit():
    for text in ("hello", "A B C", "42!", "-->"):
        assert count(text, Unit.CODEPOINT) == count(text, Unit.GRAPHEME)
        assert count(text, Unit.GRAPHEME) == count(text, Unit.AKSHARA)


def test_combining_marks_collapse_into_one_grapheme():
    assert count("é", Unit.CODEPOINT) == 2     # e + combining acute
    assert count("é", Unit.GRAPHEME) == 1


def test_emoji_zwj_sequence_is_one_grapheme():
    family = "\U0001F469‍\U0001F467"            # woman + ZWJ + girl
    assert count(family, Unit.CODEPOINT) == 3
    assert count(family, Unit.GRAPHEME) == 1


def test_trailing_virama_is_not_dropped():
    """A dangling virama has nothing to merge into; it must still be counted."""
    assert count("क्", Unit.AKSHARA) == 1
    assert "".join(segment("क्", Unit.AKSHARA)) == "क्"


# ------------------------------------------------------------------ segmenting

def test_segment_returns_the_actual_pieces():
    assert segment("abc", Unit.CODEPOINT) == ("a", "b", "c")
    assert segment(MALAYALAM_HELLO, Unit.GRAPHEME) == ("ന", "മ", "സ്കാ", "രം")


@pytest.mark.parametrize("unit", list(Unit))
def test_segmentation_is_lossless(unit):
    """Every unit must partition the text. Nothing invented, nothing dropped."""
    for text in (MALAYALAM_HELLO, TAMIL_HELLO, HINDI_HELLO, "mixed വാക്ക് 123!", ""):
        assert "".join(segment(text, unit)) == text


def test_segment_and_count_agree():
    for unit in Unit:
        assert len(segment(TAMIL_HELLO, unit)) == count(TAMIL_HELLO, unit)


@pytest.mark.parametrize("bad", [None, 42, b"bytes", ["a"]])
def test_non_string_input_is_rejected(bad):
    with pytest.raises(TypeError):
        count(bad)
    with pytest.raises(TypeError):
        measure(bad)


# ---------------------------------------------------------------- Measurement

def test_measure_reports_every_unit_at_once():
    m = measure(MALAYALAM_HELLO)

    assert isinstance(m, Measurement)
    assert (m.codepoints, m.graphemes, m.aksharas) == (8, 4, 4)
    assert m.script == "Malayalam"
    assert m.text == MALAYALAM_HELLO


def test_measurement_by_unit_matches_count():
    m = measure(TAMIL_HELLO)
    for unit in Unit:
        assert m.by(unit) == count(TAMIL_HELLO, unit)


def test_overstatement_ratio():
    m = measure(MALAYALAM_HELLO)

    assert m.codepoints_per_grapheme == 2.0     # 8 code points, 4 graphemes
    assert measure("abc").codepoints_per_grapheme == 1.0
    assert measure("").codepoints_per_grapheme == 0.0   # no division by zero


def test_measurement_is_immutable():
    m = measure("abc")
    with pytest.raises(Exception):
        m.codepoints = 99


# ------------------------------------------------------------------- script

@pytest.mark.parametrize(
    "text,expected",
    [
        (MALAYALAM_HELLO, "Malayalam"),
        (TAMIL_HELLO, "Tamil"),
        (HINDI_HELLO, "Devanagari"),
        ("hello", "Latin"),
        ("12345", None),
        ("", None),
        ("!!!", None),
    ],
)
def test_dominant_script(text, expected):
    assert dominant_script(text) == expected


def test_dominant_script_picks_the_majority():
    assert dominant_script("hi " + MALAYALAM_HELLO) == "Malayalam"


# ------------------------------------------------------------- reading speed

@pytest.mark.parametrize(
    "units,duration_ms,expected",
    [
        (20, 1000, 20.0),
        (10, 2000, 5.0),
        (0, 1000, 0.0),
        (17, 1000, 17.0),
    ],
)
def test_reading_speed(units, duration_ms, expected):
    assert reading_speed(units, duration_ms) == expected


@pytest.mark.parametrize("duration", [0, -1, -5000])
def test_reading_speed_on_bad_duration_returns_zero(duration):
    """A malformed cue must still be measurable, not abort the file."""
    assert reading_speed(10, duration) == 0.0


def test_unit_choice_changes_reading_speed_materially():
    """The finding that motivates the whole module, as an executable example."""
    text = "എന്തെങ്കിലും കുരുത്തക്കേടു കാണിച്ചാല്‍"
    m = measure(text)

    # 38 code points but only 15 grapheme clusters: a 2.53x overstatement.
    assert (m.codepoints, m.graphemes) == (38, 15)

    by_codepoint = reading_speed(m.codepoints, 1500)   # 25.3 CPS
    by_grapheme = reading_speed(m.graphemes, 1500)     # 10.0 CPS

    # Same cue, same 20 CPS threshold, opposite verdicts.
    assert by_codepoint > 20 > by_grapheme


# ------------------------------------------------------------- normalisation

def test_normalisation_is_off_by_default():
    decomposed = "é"

    assert count(decomposed, Unit.CODEPOINT) == 2
    assert count(decomposed, Unit.CODEPOINT, normalize="NFC") == 1


def test_normalisation_does_not_change_grapheme_count():
    decomposed = "é"

    assert count(decomposed, Unit.GRAPHEME) == 1
    assert count(decomposed, Unit.GRAPHEME, normalize="NFC") == 1


def test_measure_accepts_normalisation():
    assert measure("é", normalize="NFC").codepoints == 1


# ---------------------------------------------------------------- properties

_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), max_codepoint=0x0D7F),
    max_size=80,
)


@settings(max_examples=300)
@given(_text)
def test_units_are_ordered(text):
    """aksharas <= graphemes <= code points, always.

    Merging can only ever reduce the count, and a cluster is at least one code
    point. A violation would mean the segmenter invented units.
    """
    m = measure(text)

    assert m.aksharas <= m.graphemes <= m.codepoints


@settings(max_examples=300)
@given(_text)
def test_measurement_is_deterministic(text):
    assert measure(text) == measure(text)
    for unit in Unit:
        assert count(text, unit) == count(text, unit)


@settings(max_examples=300)
@given(_text)
def test_every_unit_partitions_the_text(text):
    for unit in Unit:
        assert "".join(segment(text, unit)) == text


@settings(max_examples=200)
@given(_text)
def test_empty_only_when_text_is_empty(text):
    assert (measure(text).graphemes == 0) == (text == "")
