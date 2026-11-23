"""Tour the nuon type system: report what a pipeline value arrived as,
and send one value of each type back.

Every nuon type crosses the transport as its Python counterpart —
durations as `Duration`, filesizes as `Filesize`, datetimes as `Datetime`,
binary as `bytes`, cell paths and ranges as `CellPath` and `Range`. This
script reports what it saw and returns a sample record constructed from
those types, so both directions are exercised in one call:

    2hr | py-call examples/tour.py
    {name: Alice, tags: [a b]} | py-call examples/tour.py
"""

import sys
from datetime import timezone

try:
    from nushell_nuon import (
        CellPath, Datetime, Duration, Filesize, PathMember, Range, dumps,
        loads,
    )
except ImportError:  # running the example from the repo without installing
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from nushell_nuon import (
        CellPath, Datetime, Duration, Filesize, PathMember, Range, dumps,
        loads,
    )


def report(value: object) -> dict:
    # subclass cases must precede their bases: bool and Filesize are int
    # subclasses, Duration a timedelta subclass
    match value:
        case None:
            return {"kind": "nothing"}
        case bool():
            return {"kind": "bool"}
        case Duration():
            return {"kind": "duration", "ns": value.total_ns}
        case Filesize():
            return {"kind": "filesize", "bytes": int(value)}
        case int():
            return {"kind": "int", "value": value, "hex": hex(value)}
        case float():
            return {"kind": "float", "nan": value != value}
        case str():
            return {"kind": "string", "chars": len(value)}
        case bytes():
            return {"kind": "binary", "bytes": len(value), "hex": value.hex()}
        case Datetime():
            return {
                "kind": "datetime",
                "iso": value.isoformat(),
                "utc_offset": str(value.utcoffset()),
                "nanosecond": value.nanosecond,
                "leap_second": value.leap_second,
            }
        case Range():
            return {
                "kind": "range",
                "start": value.start,
                "step": value.step,
                "end": value.end,
                "end_inclusive": value.end_inclusive,
            }
        case CellPath():
            return {
                "kind": "cell-path",
                "members": [
                    {
                        "member": member.value,
                        "optional": member.optional,
                        "insensitive": member.insensitive,
                    }
                    for member in value
                ],
            }
        case list():
            return {
                "kind": "list",
                "length": len(value),
                "elements": [report(item)["kind"] for item in value],
            }
        case dict():  # a record: the column order is part of the value
            return {"kind": "record", "columns": list(value)}
        case _:
            return {"kind": type(value).__name__}


def sample() -> dict:
    """One value of each type nushell has no native Python equivalent
    for, constructed here and sent back across the transport."""
    return {
        "for": Duration(1_500_000_000),  # -> 1500000000ns
        "disk": Filesize(2 * 1024**3),  # -> 2147483648b
        "at": Datetime(
            2026, 1, 1, 12, 0, 0, 0, timezone.utc, nanosecond=500
        ),  # -> 2026-01-01T12:00:00.000000500+00:00
        "every": Range(1, 2, 9),  # -> 1..3..9
        "where": CellPath(
            [PathMember("user"), PathMember("name", optional=True)]
        ),  # -> $.user.name?
        "blob": b"\xde\xad\xbe\xef",  # -> 0x[DEADBEEF]
    }


def main() -> int:
    request = loads(sys.stdin.read())
    print(dumps({"saw": report(request["input"]), "sample": sample()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
