"""Float formatting, replicating nushell's ``ObviousFloat``
(``crates/nu-utils/src/float.rs``): never an exponent, an integral value
gets one decimal place, anything else is the shortest round-tripping
decimal fully expanded, and the special values are ``NaN``, ``inf`` and
``-inf``.
"""

from __future__ import annotations

import decimal
import math

__all__ = ["format_float"]


def format_float(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    if value == 0.0:
        # covers -0.0 as well: the sign is kept
        return "-0.0" if math.copysign(1.0, value) < 0 else "0.0"
    if value == int(value):
        # full decimal expansion with exactly one fractional digit
        return f"{value:.1f}"
    # repr() is the shortest round-tripping decimal; expand its exponent
    text = repr(value)
    if "e" in text or "E" in text:
        text = format(decimal.Decimal(text), "f")
    return text
