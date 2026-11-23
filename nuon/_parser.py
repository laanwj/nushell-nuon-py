"""Nuon reader: document text → Python values.

The structure mirrors nushell 0.117.1's reader pipeline
(``nu-parser`` lexing/parsing + ``crates/nuon/src/from.rs`` conversion):

* A **classifier** turns one bare-word token into a value. Nuon's types form
  a closed set; each literal recognizer is a pure function returning a
  tri-state: :class:`Matched`, ``None`` (not this type — try the next), or a
  :class:`NuonDecodeError` (the token committed to this type and is a typo
  within it). The recognizer order is the Any-shape order:
  binary → range → filesize → duration → datetime → int → float, with
  keywords and structural prefixes dispatched first. This is Rust's
  Expected-vs-rest error distinction made explicit; nothing else recovers.
* Structural parsers handle records, lists, the table form and the document
  level (pipeline structure, nushell's call-vs-value rule).
* Hard errors raise immediately. The ``not supported in nuon`` family
  (interpolation, variables, subexpressions, unary minus, spreads) are
  *conversion* errors in nushell: they only surface in an otherwise-clean
  document, so they are deferred and raised at the end.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import IO, Generic, TypeVar, cast

from ._errors import NuonDecodeError
from ._lexer import (
    ASSIGN,
    COMMENT,
    EOL,
    ITEM,
    PIPE,
    PIPEPIPE,
    REDIRECT,
    SEMICOLON,
    SpanLexer,
    Token,
    lex_span,
)
from ._types import CellPath, Datetime, Duration, Filesize, PathMember, Range

__all__ = ["load", "loads"]

_MAX_DEPTH = 256
"""Nesting cap for containers. Nushell's parser recurses without a limit
(and dies on the native stack); Python needs an explicit one. A resource
limit, not part of the format (the nuon spec sanctions a cap)."""

_FRAMES_PER_LEVEL = 8
"""Python stack frames one nesting level may consume (value → container →
row/cell walkers). Only used to size the recursion-limit headroom in
:func:`loads`."""

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_UINT64_MAX = 2**64 - 1

_ASCII_DIGITS = "0123456789"
_RADIX_DIGITS = {
    2: frozenset("01"),
    8: frozenset("01234567"),
    16: frozenset("0123456789abcdefABCDEF"),
}
_RADIX_PREFIXES = (("0b", 2), ("0o", 8), ("0x", 16))


T = TypeVar("T")


@dataclass(frozen=True)
class Matched(Generic[T]):
    value: T


def _error(doc: str, msg: str, pos: int) -> NuonDecodeError:
    return NuonDecodeError(msg, doc, pos)


def _strip_underscores(text: str) -> str:
    return text.replace("_", "")


def _recognize_int(doc: str, text: str, pos: int) -> Matched[int] | NuonDecodeError | None:
    """Int literal (read_int/decode_int). ``None`` = not an int at all.

    A committed result is either "invalid digits for radix N" or a size
    error; whether the caller may recover from the former depends on the
    shape it was asked for (see ``_number_bound``).
    """
    first = next((c for c in text if c != "_"), None)
    if first is None or not (first in _ASCII_DIGITS or first in "+-"):
        return None
    stripped = _strip_underscores(text)
    if not stripped:
        return None
    sign = ""
    if stripped[0] in "+-":
        sign, stripped = stripped[0], stripped[1:]
    if not stripped:
        return None

    for prefix, radix in _RADIX_PREFIXES:
        if not stripped.startswith(prefix):
            continue
        digits = stripped[2:]
        charset = _RADIX_DIGITS[radix]
        if not digits or not all(c in charset for c in digits):
            return _error(doc, f"invalid digits for radix {radix}", pos)
        value = int(digits, radix)
        if value > _UINT64_MAX:
            return _error(doc, "number too large for 64 bits", pos)
        if sign == "-":
            if value > 2**63:
                return _error(doc, "number too large for 64 bits", pos)
            return Matched(-value)
        if sign == "+" and value > _INT64_MAX:
            return _error(doc, "number too large for 64 bits", pos)
        return Matched(value - 2**64 if value > _INT64_MAX else value)

    body = stripped  # the sign is already split off above
    if not body or not all(c in _ASCII_DIGITS for c in body):
        return None
    value = int(body)
    if sign == "-":
        value = -value
    if not _INT64_MIN <= value <= _INT64_MAX:
        return _error(doc, "number too large (add `.0` for a float)", pos)
    return Matched(value)

def _recognize_float(text: str) -> Matched[float] | None:
    """Float literal (read_float). Never commits: a bad float is a string."""
    first = next((c for c in text if c != "_"), None)
    if first is None or not (first in _ASCII_DIGITS or first in "+-.inN"):
        return None
    stripped = _strip_underscores(text)
    unsigned = stripped[1:] if stripped[0] in "+-" else stripped
    head = unsigned[:1]
    if head.isascii() and head.isalpha():
        # A special float is spelled exactly `inf`, `nan` or `NaN`, with no
        # `_` in the written form; every other alphabetic head is a string.
        written = text[1:] if text[:1] in "+-" else text
        if written not in ("inf", "nan", "NaN"):
            return None
        return Matched(float(stripped))
    if not all(c in "0123456789.eE+-" for c in stripped):
        return None
    try:
        return Matched(float(stripped))
    except ValueError:
        return None


# ------------------------------------------------------------------- units
# (suffix, convert_scale, factor): nushell's unit groups
# (crates/nu-parser/src/parse_literals.rs:1665-1786). A ``None`` scale means
# the group has no conversion (ns, b): the fractional head is dropped there,
# exactly as nushell's modf path does.

_DURATION_UNITS: tuple[tuple[str, int | None, int], ...] = (
    ("ns", None, 1),
    ("us", 1000, 1),
    ("µs", 1000, 1),  # U+00B5 micro sign
    ("μs", 1000, 1),  # U+03BC greek small letter mu — both render identically
    ("ms", 1000, 1000),
    ("sec", 1000, 1_000_000),
    ("min", 60, 1_000_000_000),
    ("hr", 60, 60_000_000_000),
    ("day", 1440, 60_000_000_000),
    ("wk", 7, 86_400_000_000_000),
)

_FILESIZE_UNITS: tuple[tuple[str, int | None, int], ...] = (
    ("KB", 1000, 1),
    ("MB", 1000, 1000),
    ("GB", 1000, 1_000_000),
    ("TB", 1000, 1_000_000_000),
    ("PB", 1000, 1_000_000_000_000),
    ("EB", 1000, 1_000_000_000_000_000),
    ("KIB", 1024, 1),
    ("MIB", 1024, 1024),
    ("GIB", 1024, 1024**2),
    ("TIB", 1024, 1024**3),
    ("PIB", 1024, 1024**4),
    ("EIB", 1024, 1024**5),
    ("B", None, 1),
)

def _is_special_float_name(text: str) -> bool:
    """Rust ``f64::from_str``'s special names: an optional sign and `inf`,
    `infinity` or `nan` in any ASCII casing."""
    body = text[1:] if text[:1] in "+-" else text
    return (
        bool(body)
        and body.isascii()
        and body.isalpha()
        and body.lower() in ("inf", "infinity", "nan")
    )


def _parse_ascii_float(text: str) -> float | None:
    """Rust ``f64::from_str``: ASCII only, plus the special float names."""
    if all(c in "0123456789.eE+-" for c in text):
        try:
            return float(text)
        except ValueError:
            return None
    if _is_special_float_name(text):
        return float(text.lower())
    return None


def _parse_ascii_int(text: str) -> int | None:
    body = text[1:] if text[:1] in "+-" else text
    if not body or not all(c in _ASCII_DIGITS for c in body):
        return None
    return int(text)


def _recognize_unit(
    doc: str,
    text: str,
    pos: int,
    table: tuple[tuple[str, int | None, int], ...],
    ty: str,
    uppercased: bool,
) -> Matched[int] | NuonDecodeError | None:
    if len(text) < 2:
        return None
    if not (
        text[0] in _ASCII_DIGITS
        or (text[0] in ".-+" and text[1] in _ASCII_DIGITS)
    ):
        return None
    value = text.upper() if uppercased else text
    for suffix, convert, factor in table:
        if value.endswith(suffix):
            break
    else:
        return None
    lhs = _strip_underscores(value[: len(value) - len(suffix)])
    if lhs.endswith("$"):
        return None
    as_float = _parse_ascii_float(lhs)
    if as_float is None:
        return _error(doc, f"{ty} value must be a number", pos)
    too_large = _error(doc, f"{ty} too large", pos)

    as_int = _parse_ascii_int(lhs)
    if as_int is not None:
        scale = (convert if convert is not None else 1) * factor
        total = as_int * scale
        if _INT64_MIN <= total <= _INT64_MAX:
            return Matched(total)
        return too_large
    # A fractional head is scaled as f64 in nushell's exact operation order
    # (parse_unit_value): (int_part * convert + frac_part * convert) * factor,
    # with the fraction dropped outright for the no-convert groups.
    frac_part, int_part = math.modf(as_float)
    if convert is None:
        num = int_part * factor
    else:
        num = (int_part * convert + frac_part * convert) * factor
    if -2.0**63 <= num < 2.0**63:
        return Matched(int(num))
    return too_large


def _recognize_duration(doc: str, text: str, pos: int) -> Matched[Duration] | NuonDecodeError | None:
    result = _recognize_unit(doc, text, pos, _DURATION_UNITS, "duration", False)
    if isinstance(result, Matched):
        return Matched(Duration(result.value))
    return result


def _recognize_filesize(doc: str, text: str, pos: int) -> Matched[Filesize] | NuonDecodeError | None:
    # The hex digit `b` would be mistaken for the unit (0x1b), so rule a
    # 0x-prefixed head out before matching (parse_filesize).
    if text.startswith("0x") or (
        text[:1] in "+-" and text[1:].startswith("0x")
    ):
        return None
    result = _recognize_unit(doc, text, pos, _FILESIZE_UNITS, "filesize", True)
    if isinstance(result, Matched):
        return Matched(Filesize(result.value))
    return result


# ---------------------------------------------------------------- datetime

_MONTH_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _valid_ymd(year: int, month: int, day: int) -> bool:
    if not 1 <= month <= 12 or day < 1:
        return False
    days = _MONTH_DAYS[month - 1]
    if month == 2 and year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
        days = 29
    return day <= days


def _exact_date_shape(text: str) -> bool:
    return (
        len(text) >= 10
        and all(c in _ASCII_DIGITS for c in text[0:4])
        and text[4] == "-"
        and all(c in _ASCII_DIGITS for c in text[5:7])
        and text[7] == "-"
        and all(c in _ASCII_DIGITS for c in text[8:10])
    )


def _is_invalid_date_literal(text: str) -> bool:
    """A token written like a date that names a day that does not exist
    (is_invalid_date_literal). Only such a token is a typo; anything longer
    or differently shaped stays a bare string."""
    if not _exact_date_shape(text):
        return False
    rest = text[10:]
    if not rest:
        time_follows = True
    elif rest[0] in "Tt":
        time_follows = (
            len(rest) >= 6
            and all(c in _ASCII_DIGITS for c in rest[1:3])
            and rest[3] == ":"
            and all(c in _ASCII_DIGITS for c in rest[4:6])
        )
    else:
        time_follows = False
    if not time_follows:
        return False
    return not _valid_ymd(int(text[0:4]), int(text[5:7]), int(text[8:10]))


def _ascii_digits(text: str, start: int, count: int) -> int | None:
    """`count` ASCII digits at `start`, or None (chrono uses ASCII only)."""
    chunk = text[start : start + count]
    if len(chunk) != count or not all(c in _ASCII_DIGITS for c in chunk):
        return None
    return int(chunk)


@dataclass(frozen=True)
class _DateTimeFields:
    """The validated fields of an RFC-3339 datetime."""

    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    micro: int
    nano: int
    tz: timezone
    leap: bool

    def to_datetime(self) -> Datetime:
        return Datetime(
            self.year, self.month, self.day, self.hour, self.minute,
            self.second, self.micro, self.tz,
            nanosecond=self.nano, leap_second=self.leap,
        )


def _parse_rfc3339_parts(text: str) -> _DateTimeFields | None:
    """A positional field parser for chrono's
    ``DateTime::parse_from_rfc3339`` as nushell exercises it:
    ``YYYY-MM-DD[Tt]HH:MM:SS[.f…](Z|z|±HH:MM)`` with seconds required, the
    ``:60`` leap second accepted, the fraction truncated at nine digits and
    offsets range-checked."""
    year = _ascii_digits(text, 0, 4)
    if year is None or text[4:5] != "-":
        return None
    month = _ascii_digits(text, 5, 2)
    if month is None or text[7:8] != "-":
        return None
    day = _ascii_digits(text, 8, 2)
    if day is None or text[10:11] not in ("T", "t"):
        return None
    hour = _ascii_digits(text, 11, 2)
    if hour is None or text[13:14] != ":":
        return None
    minute = _ascii_digits(text, 14, 2)
    if minute is None or text[16:17] != ":":
        return None
    second = _ascii_digits(text, 17, 2)
    if second is None:
        return None
    i = 19
    sub_ns = 0
    if text[i : i + 1] == ".":
        j = i + 1
        while j < len(text) and text[j] in _ASCII_DIGITS:
            j += 1
        if j == i + 1:
            return None  # a fraction needs at least one digit
        sub_ns = int(text[i + 1 : min(j, i + 10)].ljust(9, "0"))
        i = j
    offset = text[i:]
    if offset in ("Z", "z"):
        tz = timezone.utc
    else:
        if len(offset) != 6 or offset[0] not in "+-" or offset[3] != ":":
            return None
        off_h = _ascii_digits(offset, 1, 2)
        off_m = _ascii_digits(offset, 4, 2)
        if off_h is None or off_m is None or off_h > 23 or off_m > 59:
            return None
        delta = timedelta(hours=off_h, minutes=off_m)
        tz = timezone(-delta if offset[0] == "-" else delta)
    if (
        not _valid_ymd(year, month, day)
        or hour > 23
        or minute > 59
        or second > 60
    ):
        return None
    leap = second == 60
    if leap:
        second = 59
    micro, nano = divmod(sub_ns, 1000)
    return _DateTimeFields(
        year, month, day, hour, minute, second, micro, nano, tz, leap
    )


def _recognize_datetime(
    doc: str, text: str, pos: int
) -> Matched[Datetime] | NuonDecodeError | None:
    if len(text) < 6 or not all(c in _ASCII_DIGITS for c in text[0:4]) or text[4] != "-":
        return None
    for candidate in (
        text,
        text + "T00:00:00+00:00",  # date only
        text + "+00:00",  # naive time: nushell assumes UTC
    ):
        parts = _parse_rfc3339_parts(candidate)
        if parts is None:
            continue
        if parts.year == 0:
            # nushell (chrono, proleptic Gregorian) accepts year 0; Python's
            # datetime starts at 1. Loud, not silent.
            return _error(
                doc,
                "datetime year 0 is valid nuon but outside Python's "
                "representable range (0001-9999)",
                pos,
            )
        return Matched(parts.to_datetime())
    if _is_invalid_date_literal(text):
        return _error(doc, "not a valid date", pos)
    return None


# ------------------------------------------------------------------ binary

_BINARY_PREFIXES = {"0x[": (16, 2), "0o[": (8, 3), "0b[": (2, 8)}


def _recognize_binary(doc: str, text: str, pos: int) -> Matched[bytes] | NuonDecodeError | None:
    for prefix, (base, per_byte) in _BINARY_PREFIXES.items():
        if not text.startswith(prefix):
            continue
        if not text.endswith("]"):
            return None  # recoverable: "expected binary"
        # The digits are re-lexed with commas/newlines as whitespace and
        # comments ignored; semicolons are ignored, pipes are not.
        tokens, lex_err = lex_span(
            doc, pos + len(prefix), pos + len(text) - 1,
            additional_ws=",\n\r", skip_comments=True,
        )
        if lex_err is not None:
            return lex_err
        digits = []
        for t in tokens:
            if t.kind in (SEMICOLON, EOL, COMMENT):
                continue
            if t.kind != ITEM:
                return None  # recoverable: "expected binary"
            digits.append(doc[t.start : t.end])
        joined = "".join(digits)
        invalid = _error(doc, "invalid binary string", pos)
        if joined and not all(c in _RADIX_DIGITS[base] for c in joined):
            return invalid
        padding = (per_byte - len(joined) % per_byte) % per_byte
        joined = "0" * padding + joined
        try:
            data = bytes(
                int(joined[i : i + per_byte], base)
                for i in range(0, len(joined), per_byte)
            )
        except ValueError:
            return invalid
        if any(b > 255 for b in data):
            return invalid
        return Matched(data)
    return None


# ------------------------------------------------------------------- range


class _RangeDeferred:
    """A range token that parses but cannot be constructed (next == start,
    or a step out of 64-bit range): a conversion error in nushell."""

    def __init__(self, error: NuonDecodeError) -> None:
        self.error = error


def _number_bound(
    doc: str, text: str, pos: int
) -> int | float | NuonDecodeError | None:
    """A range bound: ``parse_value(Number)`` semantics. Int is tried first;
    a too-large int is committed (nushell never silently makes it a float),
    while invalid radix digits recover to a float attempt."""
    result: Matched[int] | NuonDecodeError | None = _recognize_int(doc, text, pos)
    if isinstance(result, Matched):
        return result.value
    if isinstance(result, NuonDecodeError):
        if result.msg.startswith("number too large"):
            return result
        # invalid radix digits: parse_number truncates and retries as float
    floated = _recognize_float(text)
    if isinstance(floated, Matched):
        return floated.value
    return None


def _recognize_range(
    doc: str, text: str, pos: int
) -> Matched[Range] | NuonDecodeError | _RangeDeferred | None:
    """``parse_range``: Matched | a committed bound error | None | a range
    that parses but cannot be constructed (next == start, a non-finite
    start, a step out of 64-bit range: conversion errors in nushell)."""
    if ".." not in text:
        return None
    if text.startswith("..."):
        return None
    # `..` positions outside parentheses
    depth = 0
    counted = 0
    dotdots: list[int] = []
    idx = 0
    while (found := text.find("..", idx)) != -1:
        for ch in text[counted:found]:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        counted = found
        if depth == 0:
            dotdots.append(found)
        idx = found + 2
    if len(dotdots) == 1:
        next_pos, op_pos = None, dotdots[0]
    elif len(dotdots) == 2:
        next_pos, op_pos = dotdots[0], dotdots[1]
    else:
        return None
    if next_pos is not None and next_pos > 0:
        _, err = lex_span(doc, pos, pos + next_pos, special=".?!", skip_comments=True)
        if err is not None:
            return None
    excl = text.find("..<")
    if excl != -1:
        if excl != op_pos:
            return None
        op = "..<"
        inclusive = False
    elif text[op_pos:].startswith("..="):
        op = "..="
        inclusive = True
    else:
        op = ".."
        inclusive = True

    has_from = not text.startswith("..")
    has_to = not text.endswith(op)
    start: int | float | None = None
    nxt: int | float | None = None
    end: int | float | None = None
    if has_from:
        bound = _number_bound(doc, text[:dotdots[0]], pos)
        if isinstance(bound, NuonDecodeError):
            return bound
        if bound is None:
            return None
        start = bound
    if next_pos is not None:
        next_text = text[next_pos + 2 : op_pos]
        if not next_text:
            # an empty middle bound reaches parse_value on an empty span,
            # which is a parse error, not a recoverable decline (1....5)
            return _error(
                doc, "expected a number for the range's second element",
                pos + next_pos + 2,
            )
        bound = _number_bound(doc, next_text, pos + next_pos + 2)
        if isinstance(bound, NuonDecodeError):
            return bound
        if bound is None:
            return None
        nxt = bound
    if has_to:
        bound = _number_bound(doc, text[op_pos + len(op) :], pos + op_pos + len(op))
        if isinstance(bound, NuonDecodeError):
            return bound
        if bound is None:
            return None
        end = bound
    if start is None and end is None:
        return None

    if any(isinstance(v, float) for v in (start, nxt, end)):
        start_f = float(start) if start is not None else 0.0
        if not math.isfinite(start_f):
            return _RangeDeferred(_error(doc, "cannot create a range with a non-finite start", pos))
        if nxt is not None and nxt == start_f:
            return _RangeDeferred(_error(doc, "a range's second element cannot equal its start", pos))
        if end is not None and math.isnan(float(end)):
            return _RangeDeferred(_error(doc, "cannot create a range ending in NaN", pos))
        if nxt is not None and end is not None and (nxt < start_f) != (float(end) < start_f):
            return _RangeDeferred(_error(doc, "a range's second element and end are on opposite sides of its start", pos))
        step = float(nxt) - start_f if nxt is not None else (
            1.0 if end is None or float(end) >= start_f else -1.0
        )
        return Matched(Range(start_f, step, float(end) if end is not None else None, inclusive))

    start_i = int(start) if start is not None else 0
    if nxt is not None and int(nxt) == start_i:
        return _RangeDeferred(_error(doc, "a range's second element cannot equal its start", pos))
    if nxt is not None and end is not None and (int(nxt) < start_i) != (int(end) < start_i):
        return _RangeDeferred(_error(doc, "a range's second element and end are on opposite sides of its start", pos))
    if nxt is not None:
        step = int(nxt) - start_i
        if not _INT64_MIN <= step <= _INT64_MAX:
            return _RangeDeferred(_error(doc, "a range's step does not fit in 64 bits", pos))
    else:
        step = 1 if end is None or int(end) >= start_i else -1
    return Matched(Range(start_i, step, int(end) if end is not None else None, inclusive))


# ------------------------------------------------------------------ strings

_SIMPLE_ESCAPES = {
    '"': '"', "'": "'", "\\": "\\", "/": "/",
    "(": "(", ")": ")", "{": "{", "}": "}", "$": "$", "^": "^", "#": "#",
    "|": "|", "~": "~", " ": " ",
    "a": "\a", "b": "\b", "e": "\x1b", "f": "\f",
    "n": "\n", "r": "\r", "t": "\t", "0": "\0",
}


def _unescape(doc: str, content: str, base_pos: int) -> tuple[str, NuonDecodeError | None]:
    """Process escapes in a double-quoted string body (unescape_string).
    ``\\xHH`` produces bytes that must together form valid UTF-8."""
    out = bytearray()
    i = 0
    while i < len(content):
        c = content[i]
        if c != "\\":
            out += c.encode("utf-8")
            i += 1
            continue
        escape_pos = base_pos + i
        i += 1
        if i >= len(content):
            return "", _error(doc, "incomplete escape sequence after `\\`", escape_pos)
        e = content[i]
        if e in _SIMPLE_ESCAPES:
            out += _SIMPLE_ESCAPES[e].encode("utf-8")
            i += 1
        elif e == "x":
            digits = content[i + 1 : i + 3]
            if len(digits) != 2 or not all(c in "0123456789abcdefABCDEF" for c in digits):
                return "", _error(
                    doc, "invalid hex escape `\\xHH`, expected 2 hex digits", escape_pos
                )
            out.append(int(digits, 16))
            i += 3
        elif e == "u":
            j = i + 1
            if j >= len(content) or content[j] != "{":
                return "", _error(
                    doc, "invalid unicode escape `\\u{...}`, must be 1-6 hex digits",
                    escape_pos,
                )
            k = content.find("}", j + 1)
            if k == -1:
                return "", _error(doc, "incomplete unicode escape `\\u{...}`", escape_pos)
            digits = content[j + 1 : k]
            if not 1 <= len(digits) <= 6 or not all(
                c in "0123456789abcdefABCDEF" for c in digits
            ):
                return "", _error(
                    doc, "invalid unicode escape `\\u{...}`, must be 1-6 hex digits",
                    escape_pos,
                )
            code = int(digits, 16)
            if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                return "", _error(
                    doc, "invalid unicode escape `\\u{...}`, max codepoint 0x10FFFF",
                    escape_pos,
                )
            out += chr(code).encode("utf-8")
            i = k + 1
        else:
            return "", _error(doc, f"unrecognized escape sequence `\\{e}`", escape_pos)
    try:
        return out.decode("utf-8"), None
    except UnicodeDecodeError:
        return "", _error(doc, "invalid UTF-8 from `\\x` escapes", base_pos)


def _last_quote_ok(text: str, quote: str) -> bool:
    """A quoted word runs from its opening quote to the last quote of its
    kind; text after that is an error (check_string_no_trailing_tokens)."""
    last = text.rfind(quote)
    return last == len(text) - 1 or last == 0


# ------------------------------------------------------------- the parser

_GARBAGE = object()
"""Stands in for expressions that only exist to be rejected later: the
`not supported in nuon` family is deferred and the value never escapes."""

def _is_variable_reference(rest: str) -> bool:
    """A `$name` (optionally with a `.tail` of members): an ASCII identifier
    — a letter or underscore, then letters, digits or underscores."""
    head = rest.split(".", 1)[0]
    if not head or not (head[0].isascii() and (head[0].isalpha() or head[0] == "_")):
        return False
    return all(c.isascii() and (c.isalnum() or c == "_") for c in head)


_MATH_LIKE_PREFIXES = ("(", "{", "[", "$", '"', "'", "-")

# a fake pipe token to flush the last group in the row walkers
_FLUSH = Token(PIPE, 0, 0)


def _is_spread(text: str, delimiter: str) -> bool:
    """`...` followed by a variable, a parenthesized expression or the
    container's own delimiter (extract_spread_value)."""
    return text.startswith("...") and text[3:4] in ("$", "(", delimiter)


class _Parser:
    """Parses one document. Hard errors raise immediately; conversion errors
    ("…not supported in nuon") are deferred, mirroring from.rs, where they
    only surface in an otherwise-clean document."""

    def __init__(self, doc: str) -> None:
        self.doc = doc
        self.deferred: list[NuonDecodeError] = []

    def defer(self, msg: str, pos: int) -> None:
        self.deferred.append(_error(self.doc, msg, pos))

    # ------------------------------------------------------- value parsing

    def value(self, tok: Token, depth: int) -> object:
        """Parse one value-position token: structural prefixes, keywords,
        the recognizer loop, and the string default (parse_value, Any)."""
        if depth > _MAX_DEPTH:
            raise _error(self.doc, f"document nests more than {_MAX_DEPTH} levels deep", tok.start)
        text = self.text(tok)

        if text.startswith("$"):
            return self._dollar(tok)
        if text.startswith("("):
            self.defer("subexpressions not supported in nuon", tok.start)
            return _GARBAGE
        if text.startswith("{"):
            return self._record(tok, depth + 1)
        if text.startswith("["):
            return self._list_or_table(tok, depth + 1)
        if text.startswith("r#"):
            return self._raw_string(tok)
        if text == "null":
            return None
        if text == "true":
            return True
        if text == "false":
            return False
        if text.startswith("-$") and ".." not in text:
            self.defer("unary operators not supported in nuon", tok.start)
            return _GARBAGE
        if text.startswith("-(") and text.endswith(")"):
            inner, err = lex_span(self.doc, tok.start + 2, tok.end - 1, skip_comments=True)
            if err is None:
                self.defer("unary operators not supported in nuon", tok.start)
                return _GARBAGE

        for recognize in (
            self._binary, self._range, self._filesize, self._duration,
            self._datetime, self._int, self._float,
        ):
            result = recognize(tok)
            if isinstance(result, Matched):
                return result.value
            if isinstance(result, NuonDecodeError):
                raise result
            if isinstance(result, _RangeDeferred):
                self.deferred.append(result.error)
                return _GARBAGE
        return self._string(tok)

    def text(self, tok: Token) -> str:
        return self.doc[tok.start : tok.end]

    def _int(self, tok: Token) -> Matched[int] | NuonDecodeError | None:
        return _recognize_int(self.doc, self.text(tok), tok.start)

    def _float(self, tok: Token) -> Matched[float] | None:
        return _recognize_float(self.text(tok))

    def _duration(self, tok: Token) -> Matched[Duration] | NuonDecodeError | None:
        return _recognize_duration(self.doc, self.text(tok), tok.start)

    def _filesize(self, tok: Token) -> Matched[Filesize] | NuonDecodeError | None:
        return _recognize_filesize(self.doc, self.text(tok), tok.start)

    def _datetime(self, tok: Token) -> Matched[Datetime] | NuonDecodeError | None:
        return _recognize_datetime(self.doc, self.text(tok), tok.start)

    def _binary(self, tok: Token) -> Matched[bytes] | NuonDecodeError | None:
        return _recognize_binary(self.doc, self.text(tok), tok.start)

    def _range(
        self, tok: Token
    ) -> Matched[Range] | NuonDecodeError | _RangeDeferred | None:
        return _recognize_range(self.doc, self.text(tok), tok.start)

    def _raw_string(self, tok: Token) -> str:
        """A raw string token, re-validated like parse_raw_string: the glued
        word must be exactly r#…'#."""
        text = self.text(tok)
        hashes = 0
        while 1 + hashes < len(text) and text[1 + hashes] == "#":
            hashes += 1
        if len(text) < 2 * hashes + 3:
            raise _error(self.doc, "unclosed raw string", tok.start)
        if text[-hashes:] != "#" * hashes:
            raise _error(self.doc, "unbalanced `#` in raw string", tok.start)
        if text[1 + hashes] != "'" or text[-hashes - 1] != "'":
            raise _error(self.doc, "unclosed raw string", tok.start)
        return text[hashes + 2 : -hashes - 1]

    def _string(self, tok: Token) -> object:
        """The string default (parse_string): the quoted forms and the bare
        word, with interpolation attempts deferred."""
        text = self.text(tok)
        if text.startswith('"'):
            if not _last_quote_ok(text, '"'):
                raise _error(self.doc, "extra tokens after the closing quote", tok.start)
            content, err = _unescape(self.doc, text[1:-1], tok.start + 1)
            if err is not None:
                raise err
            return content
        if text.startswith(("'", "`")):
            quote = text[0]
            if not _last_quote_ok(text, quote):
                raise _error(self.doc, "extra tokens after the closing quote", tok.start)
            return text[1:-1]
        if "(" in text:
            self.defer("string interpolation not supported in nuon", tok.start)
            return _GARBAGE
        return text

    def _dollar(self, tok: Token) -> object:
        text = self.text(tok)
        if text.startswith("$."):
            return self._cell_path(tok)
        if text.startswith(('$"', "$'")):
            self.defer("string interpolation not supported in nuon", tok.start)
            return _GARBAGE
        rest = text[1:]
        if _is_variable_reference(rest):
            if "." in rest:
                self.defer("cell paths on variables not supported in nuon", tok.start)
            else:
                self.defer("variables not supported in nuon", tok.start)
            return _GARBAGE
        raise _error(self.doc, "expected a variable after `$`", tok.start)

    def _cell_path(self, tok: Token) -> CellPath:
        """`$.a.0.b?` — members, optional `?` and case-insensitive `!`
        (parse_simple_cell_path + parse_cell_path's state machine)."""
        tokens, err = lex_span(
            self.doc, tok.start + 2, tok.end,
            additional_ws="\n\r", special=".?!", skip_comments=True,
        )
        if err is not None:
            raise err
        members: list[PathMember] = []
        # the span starts after "$.", so the first token is a member
        expecting = "member"
        for t in tokens:
            text = self.text(t)
            if expecting == "member":
                result = _recognize_int(self.doc, text, t.start)
                if isinstance(result, Matched):
                    if result.value < 0:
                        raise _error(
                            self.doc, "a negative index is not supported in a cell path", t.start
                        )
                    members.append(PathMember(result.value))
                else:
                    # a bad int is tried speculatively here: the member is
                    # the string it spells, and quoted forms are unquoted
                    if "(" in text:
                        raise _error(self.doc, "expected a string in a cell path member", t.start)
                    if text[:1] in "'\"`" and not _last_quote_ok(text, text[0]):
                        raise _error(self.doc, "extra tokens after the closing quote", t.start)
                    member = text[1:-1] if text[:1] in "'\"`" else text
                    members.append(PathMember(member))
                expecting = ".!?"
                continue
            allowed = {".": True, "!": expecting in (".!?", ".!"), "?": expecting in (".!?", ".?")}
            if not (len(text) == 1 and allowed.get(text)):
                raise _error(self.doc, f"expected {expecting!r} in a cell path", t.start)
            if text == ".":
                expecting = "member"
            elif text == "!":
                members[-1] = PathMember(
                    members[-1].value, members[-1].optional, insensitive=True
                )
                expecting = ".?"
            else:
                members[-1] = PathMember(
                    members[-1].value, optional=True, insensitive=members[-1].insensitive
                )
                expecting = ".!"
        return CellPath(members)

    # ---------------------------------------------------------- containers

    def _record(self, tok: Token, depth: int) -> dict[str, object]:
        text = self.text(tok)
        if not text.endswith("}"):
            raise _error(self.doc, "unclosed `}`", tok.start)
        lexer = SpanLexer(
            self.doc, tok.start + 1, tok.end - 1,
            additional_ws=",\n\r", skip_comments=True,
        )
        record: dict[str, object] = {}
        while True:
            key_tok = lexer.next_token(special=":")
            if key_tok is None:
                break
            if key_tok.kind != ITEM:
                raise _error(
                    self.doc,
                    f"expected a record key, found {self.text(key_tok)!r}",
                    key_tok.start,
                )
            key_text = self.text(key_tok)
            if _is_spread(key_text, "{"):
                self._spread(key_tok, record_like=True)
                continue
            key = self._field_name(key_tok)
            if key is not None and key in record:
                raise _error(
                    self.doc, f"record key {key!r} defined twice", key_tok.start
                )
            colon = lexer.next_token(special=":")
            if colon is None:
                raise _error(self.doc, "expected `:` after record key", key_tok.end)
            if colon.kind != ITEM or self.text(colon) != ":":
                raise _error(
                    self.doc,
                    f"expected `:` after record key, found {self.text(colon)!r}",
                    colon.start,
                )
            value_tok = lexer.next_token(special="")
            if value_tok is None:
                raise _error(self.doc, "expected a value for record field", colon.end)
            if value_tok.kind != ITEM:
                raise _error(
                    self.doc,
                    f"expected a value, found {self.text(value_tok)!r}",
                    value_tok.start,
                )
            value = self.value(value_tok, depth)
            if (
                isinstance(value, str)
                and not self.text(value_tok)[:1] in ("'", '"', "`")
                and ":" in self.text(value_tok)
            ):
                # A bare word with a colon would look like another
                # `key: value` field (check_record_key_or_value): quote it.
                colon_at = value_tok.start + self.text(value_tok).index(":")
                raise _error(
                    self.doc,
                    "a bare word in a record value cannot contain `:` "
                    "(quote the value)",
                    colon_at,
                )
            if key is not None:
                record[key] = value
        return record

    def _field_name(self, tok: Token) -> str | None:
        """A record key or table column (parse_field_name): a bare word is
        the string it spells, even one that is a keyword, number or date as
        a value. Other value kinds cannot name a field: None is returned and
        a deferred "only strings can be keys" is filed for it."""
        text = self.text(tok)
        if text[:1] in ("(", "[", "{", "$") or text[:2] == "r#":
            self.defer("only strings can be keys", tok.start)
            return None
        name = self._string(tok)
        return name if isinstance(name, str) else None

    def _spread(self, tok: Token, record_like: bool) -> None:
        """A `...` spread (extract_spread_value): only `...` directly before a
        variable, a parenthesized expression or the matching container is a
        spread; the value parses, then the spread itself is rejected at
        conversion time."""
        rest = self.text(tok)[3:]
        inner = Token(ITEM, tok.start + 3, tok.end)
        if rest.startswith(("$", "(")):
            self.defer("spread operator not supported in nuon", tok.start)
            return
        if record_like:
            self._record(inner, 1)
        else:
            self._list_or_table(inner, 1)
        self.defer("spread operator not supported in nuon", tok.start)

    def _list_or_table(self, tok: Token, depth: int) -> list[object]:
        text = self.text(tok)
        if not text.endswith("]"):
            raise _error(self.doc, "unclosed `]`", tok.start)
        tokens, err = lex_span(
            self.doc, tok.start + 1, tok.end - 1,
            additional_ws=",\n\r", skip_comments=True, list_items=True,
        )
        if err is not None:
            raise err
        if (
            len(tokens) >= 2
            and tokens[0].kind == ITEM
            and self.text(tokens[0]).startswith("[")
            and tokens[1].kind == SEMICOLON
        ):
            return self._table(tokens, depth)
        return self._list(tokens, depth)

    def _list(self, tokens: list[Token], depth: int) -> list[object]:
        # a `;` is never a list separator; a `||` is nushell's other reject
        for t in tokens:
            if t.kind == SEMICOLON:
                raise _error(self.doc, "unexpected semicolon in list", t.start)
        if any(t.kind == PIPEPIPE for t in tokens):
            bad = next(t for t in tokens if t.kind == PIPEPIPE)
            raise _error(self.doc, "the '||' operator is not supported in nushell", bad.start)
        items: list[object] = []
        # pipes separate the items just like whitespace does (lite_parse
        # groups them into commands and the list parser takes them all)
        group: list[Token] = []
        for t in tokens + [_FLUSH]:
            if t.kind == PIPE:
                items.extend(self._list_items(group, depth))
                group = []
            else:
                group.append(t)
        return items
    def _list_items(self, tokens: list[Token], depth: int) -> list[object]:
        items: list[object] = []
        for t in tokens:
            if t.kind == REDIRECT:
                raise _error(self.doc, "unexpected redirection", t.start)
            if t.kind == ASSIGN:
                items.append(self._string(t))
                continue
            if t.kind != ITEM:
                raise _error(self.doc, f"unexpected {self.text(t)!r} in a list", t.start)
            text = self.text(t)
            if _is_spread(text, "["):
                self._spread(t, record_like=False)
                continue
            items.append(self.value(t, depth))
        return items

    def _table(self, tokens: list[Token], depth: int) -> list[object]:
        """The table form: `[header; row, row…]` denotes a list of records
        (parse_table_expression). A header with no rows is an error; rows
        must be lists of the header's length; `;` inside a row is rejected
        by the row parse itself (a table is not a row)."""
        header_tok = tokens[0]
        # a names=True row only ever contains field names (str | None);
        # the cast records that invariant
        header = cast(
            "list[str | None]",
            self._table_row(header_tok, names=True, depth=depth),
        )
        seen: set[str] = set()
        for name in header:
            if name is not None:
                if name in seen:
                    raise _error(
                        self.doc,
                        f"table column {name!r} defined twice",
                        header_tok.start,
                    )
                seen.add(name)
        rest = tokens[2:]
        if not rest:
            raise _error(
                self.doc, "expected a table row after the header", tokens[1].end
            )
        rows = []
        for t in rest:
            text = self.text(t)
            if t.kind != ITEM or not text.startswith("["):
                raise _error(self.doc, "table item not a list", t.start)
            row = self._table_row(t, names=False, depth=depth)
            if len(row) != len(header):
                raise _error(
                    self.doc,
                    f"table row has {len(row)} columns, the header has {len(header)}",
                    t.start,
                )
            rows.append(row)
        return [
            {name: cell for name, cell in zip(header, row) if name is not None}
            for row in rows
        ]

    def _table_row(self, tok: Token, names: bool, depth: int) -> list[object]:
        """A header or body row of the table form (parse_table_row)."""
        text = self.text(tok)
        if not text.endswith("]"):
            raise _error(self.doc, "unclosed `]`", tok.start)
        tokens, err = lex_span(
            self.doc, tok.start + 1, tok.end - 1,
            additional_ws=",\n\r", skip_comments=True, list_items=True,
        )
        if err is not None:
            raise err
        cells: list[object] = []
        group: list[Token] = []
        for t in tokens + [_FLUSH]:
            if t.kind == PIPE:
                cells.extend(self._row_cells(group, names, depth))
                group = []
            else:
                group.append(t)
        return cells

    def _row_cells(self, tokens: list[Token], names: bool, depth: int) -> list[object]:
        cells: list[object] = []
        for t in tokens:
            if t.kind == SEMICOLON:
                raise _error(self.doc, "unexpected semicolon in list", t.start)
            if t.kind == REDIRECT:
                raise _error(self.doc, "unexpected redirection", t.start)
            if t.kind == ASSIGN:
                cells.append(self._string(t))
                continue
            if t.kind != ITEM:
                raise _error(self.doc, f"unexpected {self.text(t)!r} in a table", t.start)
            if _is_spread(self.text(t), "["):
                raise _error(self.doc, "cannot spread in a table row", t.start)
            if names:
                cells.append(self._field_name(t))
            else:
                cells.append(self.value(t, depth))
        return cells
    # ------------------------------------------------------------- document

    def parse(self) -> object:
        """The document level (from_nuon): structure errors first, then the
        first parse error, then the deferred conversion errors."""
        tokens, lex_err = lex_span(self.doc, 0, len(self.doc))
        pipelines = self._pipelines(tokens)
        if len(pipelines) > 1:
            first_of_second = pipelines[1][0][0]
            raise _error(
                self.doc, "excess values when loading nuon text", first_of_second.start
            )
        if lex_err is not None:
            raise lex_err
        if not pipelines:
            return None  # the empty document is `null`, as in nushell
        elements = pipelines[0]
        if len(elements) > 1:
            raise _error(
                self.doc, "detected a pipeline in nuon file", elements[1][0].start
            )
        parts = elements[0]
        if not parts:
            raise _error(self.doc, "calls not supported in nuon", 0)
        if len(parts) > 1:
            if self._is_call(parts[0]):
                raise _error(self.doc, "calls not supported in nuon", parts[0].start)
            raise _error(
                self.doc, "expected an operator after the value", parts[1].start
            )
        value = self._value_or_call(parts[0])
        if self.deferred:
            raise self.deferred[0]
        return value

    def _pipelines(self, tokens: list[Token]) -> list[list[list[Token]]]:
        """Top-level structure: eol and `;` separate pipelines, `|` separates
        the elements of one pipeline, and a trailing redirection is dropped
        together with its target (lite_parse + from_nuon's structure rules).

        A pipe at the very start of a pipeline is skipped; a pipe with
        nothing in front of it anywhere else is "unexpected end of code".
        """
        pipelines: list[list[list[Token]]] = []
        elements: list[list[Token]] = []
        parts: list[Token] = []

        def close_element() -> None:
            nonlocal parts
            elements.append(parts)
            parts = []

        def close_pipeline() -> None:
            nonlocal elements
            if elements:
                pipelines.append(elements)
            elements = []

        i = 0
        last_pipe: Token | None = None
        while i < len(tokens):
            t = tokens[i]
            i += 1
            if t.kind == COMMENT:
                continue
            if t.kind in (EOL, SEMICOLON):
                if parts:
                    close_element()
                close_pipeline()
                last_pipe = None
                continue
            if t.kind == PIPEPIPE:
                raise _error(
                    self.doc, "the '||' operator is not supported in nushell", t.start
                )
            if t.kind == PIPE:
                last_pipe = t
                if not parts and not elements:
                    continue  # a pipe at the very start: nothing to close
                if not parts:
                    # nothing in front of it: the element it would close is
                    # empty (`a | | b`) or already closed
                    raise _error(self.doc, "unexpected end of code", t.start)
                close_element()
                continue
            if t.kind == REDIRECT:
                # the redirection and its target are not parts of the value
                if i < len(tokens) and tokens[i].kind == ITEM:
                    i += 1
                last_pipe = None
                continue
            last_pipe = None
            parts.append(t)
        if parts:
            close_element()
        close_pipeline()
        if last_pipe is not None:
            # a pipe with nothing after it: the pipeline element is incomplete
            raise _error(self.doc, "unexpected end of code", last_pipe.start)
        return pipelines
    def _is_call(self, tok: Token) -> bool:
        """The nushell call rule: a bare word that is not a value (not
        math-like, not a literal, not a keyword) is a command call."""
        text = self.text(tok)
        if text[:1] in _MATH_LIKE_PREFIXES or text[:2] == "r#":
            return False
        if text in ("true", "false", "null"):
            return False
        if self._literal_like(tok):
            return False
        return True

    def _literal_like(self, tok: Token) -> bool:
        """is_literal_like: a token a literal parser accepts, or rejects as
        an invalid/too-large literal of its kind, is a value; anything else
        is a command name."""
        text = self.text(tok)
        # number: int, then float; invalid radix digits recover to the float
        result = _recognize_int(self.doc, text, tok.start)
        if isinstance(result, Matched) or (
            isinstance(result, NuonDecodeError) and result.msg.startswith("number too large")
        ):
            return True
        if _recognize_float(text) is not None:
            return True
        for recognizer, ty in (
            (_recognize_filesize, "filesize"),
            (_recognize_duration, "duration"),
        ):
            unit = recognizer(self.doc, text, tok.start)
            if isinstance(unit, Matched) or (
                isinstance(unit, NuonDecodeError) and unit.msg == f"{ty} too large"
            ):
                return True
        if _recognize_datetime(self.doc, text, tok.start) is not None:
            return True
        if _recognize_binary(self.doc, text, tok.start) is not None:
            return True
        if ".." in text and _recognize_range(
            self.doc, text, tok.start
        ) is not None:
            return True
        return False

    def _value_or_call(self, tok: Token) -> object:
        text = self.text(tok)
        if text in ("not", "if", "match"):
            raise _error(self.doc, f"`{text}` is not valid in a nuon document", tok.start)
        if self._is_call(tok):
            raise _error(self.doc, "calls not supported in nuon", tok.start)
        return self.value(tok, 0)


# ------------------------------------------------------------- public API


def loads(s: str | bytes | bytearray) -> object:
    """Deserialize nuon text (a string, or UTF-8 bytes) to a Python value.

    Raises :class:`NuonDecodeError` on invalid input — never silently
    repairs or reinterprets it.
    """
    if isinstance(s, (bytes, bytearray)):
        try:
            s = bytes(s).decode("utf-8")
        except UnicodeDecodeError as e:
            raise NuonDecodeError(
                f"document is not valid UTF-8 (byte {e.start})", "", 0
            ) from None
    # exactly one leading byte order mark is stripped, never an inner one
    if s.startswith("﻿"):
        s = s[1:]
    parser = _Parser(s)
    # The nesting cap needs headroom on Python's own recursion limit; the
    # bump is restored no matter how parsing ends.
    limit = sys.getrecursionlimit()
    needed = _MAX_DEPTH * _FRAMES_PER_LEVEL + 100
    if limit < needed:
        sys.setrecursionlimit(needed)
    try:
        return parser.parse()
    finally:
        sys.setrecursionlimit(limit)


def load(fp: IO[str] | IO[bytes]) -> object:
    """Deserialize nuon text from a readable file-like object."""
    return loads(fp.read())

