"""Python representations of the nuon value types Python has no native
equivalent for.

Mapping (see the README for the full table):

==================  ==========================================
nuon type           Python
==================  ==========================================
duration            :class:`Duration` (ns; timedelta subclass)
filesize            :class:`Filesize` (bytes; int subclass)
datetime            :class:`Datetime` (datetime subclass)
cell-path           :class:`CellPath` (of :class:`PathMember`)
range               :class:`Range`
==================  ==========================================

``timedelta`` and ``datetime`` are microsecond-resolution, so the two
subclasses carry the sub-microsecond remainder in a ``nanosecond`` attribute
(0-999). ``Datetime`` additionally carries a ``leap_second`` flag for the
``:60`` spelling, which ``datetime`` cannot represent on its own.

Arithmetic on the subclasses returns the plain base type and drops the
extras; that is inherited behavior and documented, not a bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Iterable, Iterator

__all__ = ["CellPath", "Datetime", "Duration", "Filesize", "PathMember", "Range"]

# datetime and timedelta normalize to non-negative seconds/microseconds with
# the sign carried by `days`, so a negative duration decomposes as
# floor-division microseconds plus a non-negative nanosecond remainder.
_NSPERUS = 1000


class Filesize(int):
    """A nuon filesize: a count of bytes (possibly negative, as in nushell)."""

    def __repr__(self) -> str:
        return f"Filesize({int.__repr__(self)})"


class Duration(timedelta):
    """A nuon duration: a count of nanoseconds.

    Constructed from the nanosecond count: ``Duration(1_500)`` is 1.5µs.
    ``d.nanosecond`` is the 0-999 remainder below microsecond resolution and
    ``d.total_ns`` the exact nanosecond count.
    """

    nanosecond: int

    def __new__(cls, nanoseconds: int) -> Duration:
        nanoseconds = int(nanoseconds)
        ns = nanoseconds % _NSPERUS
        micros = (nanoseconds - ns) // _NSPERUS
        self = super().__new__(cls, microseconds=micros)
        self.nanosecond = ns
        return self

    @property
    def total_ns(self) -> int:
        return (
            (self.days * 86_400 + self.seconds) * 1_000_000_000
            + self.microseconds * _NSPERUS
            + self.nanosecond
        )

    def __repr__(self) -> str:
        return f"Duration({self.total_ns})"


class Datetime(datetime):
    """A nuon datetime: an instant plus a UTC offset, nanosecond precision.

    The written second ``:60`` (leap second) is stored as second 59 with
    ``leap_second=True``. ``nanosecond`` is the 0-999 remainder below
    microsecond resolution.
    """

    nanosecond: int
    leap_second: bool

    def __new__(
        cls,
        year: int,
        month: int,
        day: int,
        hour: int = 0,
        minute: int = 0,
        second: int = 0,
        microsecond: int = 0,
        tzinfo: tzinfo | None = None,
        *,
        nanosecond: int = 0,
        leap_second: bool = False,
    ) -> Datetime:
        self = super().__new__(
            cls, year, month, day, hour, minute, second, microsecond, tzinfo
        )
        self.nanosecond = nanosecond
        self.leap_second = leap_second
        return self

    def __repr__(self) -> str:
        text = self._iso()
        if self.leap_second:
            text = f"{text}, leap_second=True"
        return f"Datetime({text!r})"

    def _iso(self) -> str:
        """Full-precision ISO form (always 9 fractional digits when any
        sub-second part exists, always an offset) — used by repr."""
        base = f"{self.year:04}-{self.month:02}-{self.day:02}T{self.hour:02}:{self.minute:02}"
        second = self.second + 1 if self.leap_second else self.second
        sub = self.microsecond * _NSPERUS + self.nanosecond
        base += f":{second:02}" if sub == 0 else f":{second:02}.{sub:09d}"
        offset = self.utcoffset()
        if offset is None:
            return base + "+00:00"
        total = int(offset.total_seconds())
        sign = "-" if total < 0 else "+"
        total = abs(total)
        return f"{base}{sign}{total // 3600:02}:{total % 3600 // 60:02}"


@dataclass(frozen=True)
class PathMember:
    """One cell-path member: a record key (str) or list index (int).

    ``optional`` marks the ``?`` suffix, ``insensitive`` the ``!`` suffix
    (case-insensitive matching, nushell's explicit casing syntax).
    """

    value: str | int
    optional: bool = False
    insensitive: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, (str, int)):
            raise TypeError("PathMember.value must be a str or int, not bool")
        if isinstance(self.value, int) and self.insensitive:
            # nushell's PathMember::Int has no casing: an index has no
            # spelling to be case-insensitive about, and the writer drops
            # the flag, so such a member could never round-trip
            raise ValueError("an int cell-path member cannot be insensitive")


def _member(value: PathMember | str | int) -> PathMember:
    if isinstance(value, PathMember):
        return value
    return PathMember(value)


class CellPath:
    """A nuon cell-path: ``$.a.0.b?`` is ``CellPath([PathMember('a'),
    PathMember(0), PathMember('b', optional=True)])``. ``$.`` is the empty
    path ``CellPath()``."""

    members: tuple[PathMember, ...]

    def __init__(self, members: Iterable[PathMember | str | int] = ()) -> None:
        self.members = tuple(_member(m) for m in members)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CellPath) and self.members == other.members

    def __hash__(self) -> int:
        return hash(self.members)

    def __len__(self) -> int:
        return len(self.members)

    def __iter__(self) -> Iterator[PathMember]:
        return iter(self.members)

    def __repr__(self) -> str:
        return f"CellPath({list(self.members)!r})"


@dataclass(frozen=True)
class Range:
    """A nuon range.

    ``start`` defaults to 0 and ``end`` ``None`` means unbounded (``1..``).
    ``step`` is the increment (the literal's middle value is the *second*
    element, so ``1..3..9`` is start 1, step 2). ``end_inclusive`` selects
    ``end`` vs ``<end``. Any float bound makes it a float range, as in
    nushell.
    """

    start: int | float = 0
    step: int | float = 1
    end: int | float | None = None
    end_inclusive: bool = True

    def __post_init__(self) -> None:
        if self.step == 0:
            raise ValueError("a nuon range cannot have a step of zero")
