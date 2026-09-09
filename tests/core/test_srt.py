from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bhashasub.core.formats.detect import decode
from bhashasub.core.formats.srt import parse, serialize
from bhashasub.core.model import Cue, TimeCode, Track

MALAYALAM = "അത് നടക്കുമെന്ന് എനിക്ക് തോന്നുന്നില്ല."

BASIC = """1
00:00:01,000 --> 00:00:03,000
Hello there.

2
00:00:04,000 --> 00:00:07,500
Second line one
Second line two
"""


# ---------------------------------------------------------------- parsing

def test_parses_a_basic_file():
    result = parse(BASIC)

    assert result.ok
    assert len(result.track) == 2

    first, second = result.track
    assert first.index == 1
    assert first.start == TimeCode(1_000)
    assert first.end == TimeCode(3_000)
    assert first.lines == ("Hello there.",)

    assert second.lines == ("Second line one", "Second line two")
    assert second.duration_ms == 3_500


def test_source_spans_point_at_the_right_lines():
    """SARIF reporting depends on these being correct."""
    result = parse(BASIC)

    assert result.track[0].span.start_line == 1
    assert result.track[0].span.end_line == 3
    assert result.track[1].span.start_line == 5
    assert result.track[1].span.end_line == 8


def test_track_metadata_is_carried_through():
    result = parse(BASIC, fps=25.0, language="ml", encoding="utf-8")

    assert result.track.fps == 25.0
    assert result.track.language == "ml"
    assert result.track.encoding == "utf-8"
    assert result.track.format == "srt"


def test_handles_crlf_via_decode():
    raw = BASIC.replace("\n", "\r\n").encode("utf-8")

    result = parse(decode(raw).text)

    assert len(result.track) == 2
    assert result.track[0].lines == ("Hello there.",)


def test_handles_bom():
    result = parse("﻿" + BASIC)

    assert len(result.track) == 2
    assert result.track[0].index == 1


def test_unicode_content_is_preserved():
    text = f"1\n00:00:01,000 --> 00:00:03,000\n{MALAYALAM}\n"

    result = parse(text)

    assert result.track[0].lines == (MALAYALAM,)


def test_trailing_blank_lines_and_extra_separators():
    text = "\n\n1\n00:00:01,000 --> 00:00:03,000\nHi\n\n\n\n"

    result = parse(text)

    assert len(result.track) == 1
    assert result.track[0].lines == ("Hi",)


def test_empty_input_yields_an_empty_track():
    for text in ("", "   ", "\n\n\n"):
        result = parse(text)
        assert len(result.track) == 0
        assert result.ok


# --------------------------------------------------------------- resilience

def test_cue_without_an_index_is_accepted_and_numbered():
    text = "00:00:01,000 --> 00:00:03,000\nHi\n"

    result = parse(text)

    assert len(result.track) == 1
    assert result.track[0].index == 1
    assert any("no index" in issue.message for issue in result.issues)
    assert all(issue.severity == "warning" for issue in result.issues)


def test_block_without_a_timing_line_is_skipped_and_reported():
    text = "1\nJust some text\n\n2\n00:00:04,000 --> 00:00:05,000\nOK\n"

    result = parse(text)

    assert len(result.track) == 1          # parsing continued past the bad block
    assert result.track[0].index == 2
    assert not result.ok
    assert result.issues[0].line == 1
    assert "no timing line" in result.issues[0].message


def test_unreadable_timestamp_is_reported_not_raised():
    text = "1\n99:99:99,999 --> 00:00:03,000\nHi\n\n2\n00:00:04,000 --> 00:00:05,000\nOK\n"

    result = parse(text)

    assert len(result.track) == 1
    assert not result.ok
    assert "timestamp" in result.issues[0].message.lower()


def test_trailing_text_on_the_timing_line_is_a_warning_not_a_loss():
    text = "1\n00:00:01,000 --> 00:00:03,000 X1:012 Y1:400\nHi\n"

    result = parse(text)

    assert len(result.track) == 1
    assert result.ok                        # warnings do not make it "not ok"
    assert any("trailing" in i.message for i in result.issues)


def test_numeric_first_line_of_dialogue_is_not_eaten_as_an_index():
    text = "1\n00:00:01,000 --> 00:00:03,000\n42\n"

    result = parse(text)

    assert result.track[0].lines == ("42",)


def test_inverted_timing_is_kept_for_the_rules_to_flag():
    """A backwards cue is a rule violation, not a parse failure."""
    text = "1\n00:00:05,000 --> 00:00:01,000\nHi\n"

    result = parse(text)

    assert len(result.track) == 1
    assert result.track[0].duration_ms == -4_000
    assert not result.track[0].has_valid_timing


def test_cue_with_no_text_is_kept():
    text = "1\n00:00:01,000 --> 00:00:03,000\n\n2\n00:00:04,000 --> 00:00:05,000\nOK\n"

    result = parse(text)

    assert len(result.track) == 2
    assert result.track[0].is_empty


def test_garbage_input_does_not_crash():
    for text in ("\x00\x01\x02", "-->", "1\n2\n3\n", "-" * 5000, "00:00:01,000-->"):
        parse(text)     # must not raise


# -------------------------------------------------------------- serializing

def test_serialize_produces_canonical_srt():
    result = parse(BASIC)

    assert serialize(result.track) == BASIC


def test_serialize_empty_track():
    assert serialize(Track()) == ""


def test_serialize_preserves_indices_by_default():
    track = Track(cues=(Cue(7, TimeCode(0), TimeCode(1_000), ("Hi",)),))

    assert serialize(track).startswith("7\n")


def test_renumber_is_opt_in():
    track = Track(cues=(Cue(7, TimeCode(0), TimeCode(1_000), ("Hi",)),))

    assert serialize(track, renumber=True).startswith("1\n")


def test_serialize_ends_with_exactly_one_newline():
    output = serialize(parse(BASIC).track)

    assert output.endswith("\n")
    assert not output.endswith("\n\n")


# ---------------------------------------------------- property-based testing

_line = (
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs", "Cc"), max_codepoint=0x0D7F),
        min_size=1,
        max_size=40,
    )
    .map(lambda s: s.strip())
    .filter(lambda s: s and "-->" not in s)
)


@st.composite
def tracks(draw):
    """Generate tracks that SRT can actually represent.

    Blank lines inside a cue are excluded because the SRT format itself uses a
    blank line as the cue separator and therefore cannot round-trip them. That
    is a limitation of the format, not of this parser.
    """
    count = draw(st.integers(min_value=0, max_value=6))
    cues = []
    for position in range(count):
        start = draw(st.integers(min_value=0, max_value=10_000_000))
        length = draw(st.integers(min_value=0, max_value=20_000))
        lines = draw(st.lists(_line, min_size=1, max_size=3))
        cues.append(
            Cue(
                index=position + 1,
                start=TimeCode(start),
                end=TimeCode(start + length),
                lines=tuple(lines),
            )
        )
    return Track(cues=tuple(cues), format="srt")


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(tracks())
def test_parse_is_a_retraction(track):
    """The correct round-trip law: parse(serialize(parse(s))) == parse(s).

    Note this is NOT parse(serialize(track)) == track, which is false in general
    because serialization legitimately normalises.
    """
    once = parse(serialize(track)).track
    twice = parse(serialize(once)).track

    assert once == twice


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(tracks())
def test_serialize_is_deterministic(track):
    """Byte-identical output is what makes subtitle files diff cleanly in git."""
    assert serialize(track) == serialize(track)


@settings(max_examples=200)
@given(st.text(max_size=400))
def test_parse_never_raises_on_arbitrary_text(text):
    result = parse(text)

    assert isinstance(result.track, Track)


@settings(max_examples=100)
@given(tracks())
def test_serialized_output_reparses_to_the_same_cue_count(track):
    result = parse(serialize(track))

    assert len(result.track) == len(track)


def test_blank_line_inside_a_cue_cannot_round_trip():
    """Documents a real format limitation rather than pretending it works."""
    track = Track(cues=(Cue(1, TimeCode(0), TimeCode(1_000), ("a", "", "b")),))

    reparsed = parse(serialize(track)).track

    assert len(reparsed) == 1
    assert reparsed[0].lines == ("a",)      # "b" became a separate malformed block
