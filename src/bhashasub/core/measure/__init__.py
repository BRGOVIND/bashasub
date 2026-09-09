"""Script-aware measurement of subtitle text.

Grapheme clusters are the default unit. See docs/measurement-study.md for the
evidence behind that choice.
"""

from .scripts import VIRAMAS, ZERO_WIDTH_JOINERS, dominant_script
from .units import (
    DEFAULT_UNIT,
    Measurement,
    Unit,
    count,
    measure,
    reading_speed,
    segment,
)

__all__ = [
    "Unit",
    "Measurement",
    "DEFAULT_UNIT",
    "segment",
    "count",
    "measure",
    "reading_speed",
    "dominant_script",
    "VIRAMAS",
    "ZERO_WIDTH_JOINERS",
]
