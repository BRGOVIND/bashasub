import pytest

from bhashasub.core.model import (
    Cue,
    SourceSpan,
    SubtitleError,
    TimeCode,
    TimeCodeError,
    Track,
    frames_to_ms,
    ms_to_frames,
)


# ------------------------------------------------------------------ TimeCode

@pytest.mark.parametrize(
    "text,expected_ms",
    [
        ("00:00:00,000", 0),
        ("00:00:01,000", 1_000),
        ("00:00:01.000", 1_000),          # WebVTT decimal separator
        ("01:02:03,004", 3_723_004),
        ("00:01:00,000", 60_000),
        ("10:00:00,000", 36_000_000),
        ("01:02.500", 62_500),            # hours omitted
        ("00:00:00,5", 500),              # short fraction is left-padded
        ("00:00:00,05", 50),
        ("100:00:00,000", 360_000_000),   # programmes longer than 99 hours
        ("00:00", 0),                     # bare MM:SS is valid WebVTT
        ("12:34", 754_000),
    ],
)
def test_parse_accepts_real_world_timecodes(text, expected_ms):
    assert TimeCode.parse(text).ms == expected_ms


@pytest.mark.parametrize(
    "text",
    ["", "   ", "not a time", "00:00:00:00", "aa:bb:cc,ddd", "00:60:00,000",
     "00:00:60,000", "::", "1:2:3:4,5"],
)
def test_parse_rejects_malformed_timecodes(text):
    with pytest.raises(TimeCodeError):
        TimeCode.parse(text)


def test_timecode_rejects_non_integer_milliseconds():
    with pytest.raises(TimeCodeError):
        TimeCode(1.5)
    with pytest.raises(TimeCodeError):
        TimeCode(True)          # bool is an int subclass; must still be rejected
    with pytest.raises(TimeCodeError):
        TimeCode(-1)


@pytest.mark.parametrize(
    "text", ["00:00:00,000", "01:02:03,004", "23:59:59,999", "00:00:00,001"]
)
def test_srt_rendering_round_trips(text):
    assert TimeCode.parse(text).to_srt() == text


def test_vtt_rendering_uses_a_dot():
    assert TimeCode.parse("01:02:03,004").to_vtt() == "01:02:03.004"


def test_timecode_ordering_and_arithmetic():
    a, b = TimeCode(1_000), TimeCode(3_000)

    assert a < b
    assert max(a, b) == b
    assert b - a == 2_000                 # TimeCode - TimeCode is a duration
    assert a + 500 == TimeCode(1_500)     # TimeCode + int shifts
    assert isinstance(a + 500, TimeCode)


# -------------------------------------------------------------------- frames

@pytest.mark.parametrize(
    "frames,fps,expected",
    [
        (2, 25, 80),        # exact
        (2, 24, 84),        # 83.33 -> rounded up
        (2, 23.976, 84),    # 83.42 -> rounded up
        (2, 29.97, 67),     # 66.73 -> rounded up
        (0, 25, 0),
        (1, 25, 40),
    ],
)
def test_frames_to_ms_rounds_up(frames, fps, expected):
    """Frame counts in specs are minimums, so rounding must never undershoot."""
    assert frames_to_ms(frames, fps) == expected


def test_ms_to_frames_rounds_down():
    assert ms_to_frames(80, 25) == 2
    assert ms_to_frames(79, 25) == 1
    assert ms_to_frames(0, 25) == 0


@pytest.mark.parametrize("fps", [0, -1, -25.0])
def test_frame_conversion_rejects_invalid_fps(fps):
    with pytest.raises(TimeCodeError):
        frames_to_ms(1, fps)
    with pytest.raises(TimeCodeError):
        ms_to_frames(1000, fps)


def test_frames_to_ms_rejects_negative_frames():
    with pytest.raises(TimeCodeError):
        frames_to_ms(-1, 25)


def test_timecode_from_frames():
    assert TimeCode.from_frames(25, 25) == TimeCode(1_000)


# ----------------------------------------------------------------- SourceSpan

def test_source_span_is_one_based_and_ordered():
    span = SourceSpan(start_line=1, end_line=3)
    assert span.start_line == 1

    with pytest.raises(SubtitleError):
        SourceSpan(start_line=0, end_line=1)
    with pytest.raises(SubtitleError):
        SourceSpan(start_line=5, end_line=4)


# ----------------------------------------------------------------------- Cue

def _cue(index=1, start=1_000, end=3_000, lines=("Hello",), span=None):
    return Cue(index, TimeCode(start), TimeCode(end), lines, span)


def test_cue_basic_properties():
    cue = _cue(lines=("Hello", "world"))

    assert cue.duration_ms == 2_000
    assert cue.text == "Hello\nworld"
    assert not cue.is_empty
    assert cue.has_valid_timing


def test_cue_detects_empty_and_inverted():
    assert _cue(lines=()).is_empty
    assert _cue(lines=("", "   ")).is_empty
    assert not _cue(start=3_000, end=1_000).has_valid_timing
    assert _cue(start=3_000, end=1_000).duration_ms == -2_000
    assert not _cue(start=1_000, end=1_000).has_valid_timing


def test_cue_gap_and_overlap():
    first = _cue(index=1, start=0, end=1_000)
    second = _cue(index=2, start=1_200, end=2_000)
    overlapping = _cue(index=3, start=800, end=2_000)

    assert first.gap_to(second) == 200
    assert first.gap_to(overlapping) == -200      # negative means overlap
    assert not first.overlaps(second)
    assert first.overlaps(overlapping)


def test_touching_cues_do_not_overlap():
    """A cue ending exactly when the next begins is a zero gap, not an overlap."""
    first = _cue(index=1, start=0, end=1_000)
    second = _cue(index=2, start=1_000, end=2_000)

    assert first.gap_to(second) == 0
    assert not first.overlaps(second)


def test_span_is_excluded_from_equality():
    """Provenance must not affect identity, or the round-trip property breaks."""
    a = _cue(span=SourceSpan(1, 3))
    b = _cue(span=SourceSpan(90, 92))

    assert a == b


def test_cue_is_immutable():
    cue = _cue()
    with pytest.raises(Exception):
        cue.index = 5

    assert cue.replace(index=7).index == 7
    assert cue.index == 1


# --------------------------------------------------------------------- Track

def test_empty_track():
    track = Track()

    assert len(track) == 0
    assert track.duration_ms == 0
    assert track.is_ordered
    assert list(track.pairs()) == []


def test_track_sequence_behaviour():
    cues = (_cue(1, 0, 1_000), _cue(2, 2_000, 3_000))
    track = Track(cues=cues)

    assert len(track) == 2
    assert track[0].index == 1
    assert [c.index for c in track] == [1, 2]
    assert track.duration_ms == 3_000
    assert track.is_ordered
    assert len(list(track.pairs())) == 1


def test_track_detects_unordered_cues():
    track = Track(cues=(_cue(1, 5_000, 6_000), _cue(2, 0, 1_000)))

    assert not track.is_ordered


def test_with_cues_returns_a_new_track():
    track = Track(cues=(_cue(),), language="ml")
    updated = track.with_cues(())

    assert len(track) == 1
    assert len(updated) == 0
    assert updated.language == "ml"
