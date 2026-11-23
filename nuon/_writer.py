"""Nuon writer: Python values → nuon text, byte-identical to nushell's
``to nuon`` (``crates/nuon/src/to.rs`` + ``nu-utils`` quoting/float) for the
same value and flags.

The four styles and their flag precedence mirror the command:

- default (compact): ``[1, 2]``, ``{a: 1}``
- ``raw``: no whitespace at all (wins over every indent form; ``indent 0``
  and ``tabs 0`` mean the same thing)
- ``indent N`` / ``tabs N`` / ``pretty`` (= indent 2): one item per line,
  aligned table columns; ``tabs`` wins over ``indent``
- ``commas=False`` separates with whitespace instead; it contradicts
  ``raw`` and raises
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import IO, Any, Iterable, cast

from ._errors import NuonEncodeError
from ._float import format_float
from ._quoting import as_raw_string, escape_quote_string, needs_quoting
from ._types import CellPath, Datetime, Duration, Filesize, PathMember, Range
from ._unicode import display_width

__all__ = ["dump", "dumps"]

_MAX_DEPTH = 256
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def dumps(
    obj: object,
    *,
    indent: int | None = None,
    tabs: int | None = None,
    pretty: bool = False,
    raw: bool = False,
    raw_strings: bool = False,
    list_of_records: bool = False,
    commas: bool = True,
) -> str:
    """Serialize `obj` to nuon text.

    See the module docstring for the flag semantics; the defaults produce
    the compact single-line form. Raises :class:`NuonEncodeError` (with the
    path of the offending value) or ``TypeError`` for values nuon cannot
    express — never silently drops or rounds anything.
    """
    if raw and not commas:
        raise ValueError(
            "raw and commas=False are incompatible: `--raw` removes the "
            "whitespace `--no-commas` uses as its separator"
        )
    if raw:
        indentation = ""
    elif tabs is not None:
        if tabs == 0:
            indentation = ""
        else:
            indentation = "\t" * tabs
    elif indent is not None:
        indentation = "" if indent == 0 else " " * indent
    elif pretty:
        indentation = " " * 2
    else:
        indentation = None

    return _Writer(
        indentation=indentation,
        raw_strings=raw_strings,
        list_of_records=list_of_records,
        commas=commas,
    ).write(obj, "$")


def dump(obj: object, fp: IO[str], **kwargs: object) -> None:
    """Serialize `obj` as nuon text to a writable file-like object."""
    fp.write(dumps(obj, **kwargs))  # type: ignore[arg-type]


class _Writer:
    def __init__(
        self,
        indentation: str | None,
        raw_strings: bool,
        list_of_records: bool,
        commas: bool,
    ) -> None:
        self.indentation = indentation
        self.raw_strings = raw_strings
        self.list_of_records = list_of_records
        self.commas = commas

    # separators by style (get_true_separators)
    @property
    def _nl(self) -> str:
        return "" if self.indentation is None or self.indentation == "" else "\n"

    @property
    def _sep(self) -> str:
        if self.indentation is None or self.indentation == "":
            return " " if self.indentation is None else ""
        return ""

    @property
    def _kv_sep(self) -> str:
        # raw drops the space after the colon; every other style keeps it
        return "" if self.indentation == "" else " "

    def _idt(self, depth: int) -> str:
        return (self.indentation or "") * depth

    def _fail(self, msg: str, path: str) -> NuonEncodeError:
        return NuonEncodeError(msg, path)

    # ------------------------------------------------------------- scalars

    def _scalar(self, obj: object, path: str) -> str:
        if obj is None:
            return "null"
        if isinstance(obj, bool):
            return "true" if obj else "false"
        if isinstance(obj, Filesize):
            if not _INT64_MIN <= obj <= _INT64_MAX:
                raise self._fail("filesize does not fit in 64 bits", path)
            return f"{int(obj)}b"
        if isinstance(obj, int):
            if not _INT64_MIN <= obj <= _INT64_MAX:
                raise self._fail(
                    f"int {obj} does not fit in 64 bits "
                    "(write it as a float with a `.0` to lose precision)",
                    path,
                )
            return str(obj)
        if isinstance(obj, float):
            return format_float(obj)
        if isinstance(obj, str):
            # a lone string is always quoted: a bare word at the top level
            # would read back as a command call
            return self._quote(obj)
        if isinstance(obj, (bytes, bytearray)):
            return f"0x[{bytes(obj).hex().upper()}]"
        if isinstance(obj, Duration):
            return f"{obj.total_ns}ns"
        if isinstance(obj, timedelta):
            # plain timedeltas carry microseconds at most
            total_us = obj.days * 86400_000_000 + obj.seconds * 1_000_000 + obj.microseconds
            return f"{total_us * 1000}ns"
        if isinstance(obj, datetime):
            return self._datetime(obj, path)
        if isinstance(obj, CellPath):
            return self._cell_path(obj)
        if isinstance(obj, Range):
            return self._range(obj)
        raise TypeError(f"cannot encode {type(obj).__name__} as nuon")

    def _quote(self, s: str) -> str:
        if self.raw_strings:
            raw = as_raw_string(s)
            if raw is not None:
                return raw
        return escape_quote_string(s)

    def _datetime(self, dt: datetime, path: str) -> str:
        offset = dt.utcoffset()
        if offset is None:
            offset = timedelta(0)  # a naive datetime is written as UTC
        total = int(offset.total_seconds())
        if total % 60 or abs(total) >= 24 * 3600:
            raise self._fail(
                f"datetime offset {offset} is not a whole number of minutes "
                "within ±23:59",
                path,
            )
        sign = "-" if total < 0 else "+"
        total = abs(total)
        second = dt.second + 1 if getattr(dt, "leap_second", False) else dt.second
        sub_ns = dt.microsecond * 1000 + getattr(dt, "nanosecond", 0)
        if sub_ns == 0:
            fraction = ""
        elif sub_ns % 1_000_000 == 0:
            fraction = f".{sub_ns // 1_000_000:03d}"
        elif sub_ns % 1000 == 0:
            fraction = f".{sub_ns // 1000:06d}"
        else:
            fraction = f".{sub_ns:09d}"
        return (
            f"{dt.year:04}-{dt.month:02}-{dt.day:02}"
            f"T{dt.hour:02}:{dt.minute:02}:{second:02}{fraction}"
            f"{sign}{total // 3600:02}:{total % 3600 // 60:02}"
        )

    def _cell_path(self, cp: CellPath) -> str:
        out = ["$"]
        for member in cp.members:
            out.append(f".{self._member(member)}")
        if not cp.members:
            out.append(".")  # the empty cell path is `$.`, not `$`
        return "".join(out)

    def _member(self, member: PathMember) -> str:
        if isinstance(member.value, int):
            return f"{member.value}{'?' if member.optional else ''}"
        text = escape_quote_string(member.value) if needs_quoting(member.value) else member.value
        return (
            f"{text}{'!' if member.insensitive else ''}{'?' if member.optional else ''}"
        )

    def _range(self, r: Range) -> str:
        is_float = any(
            isinstance(v, float) for v in (r.start, r.step, r.end)
        )
        number = format_float if is_float else str
        out = [f"{number(r.start)}.."]
        step = r.step
        show_step = step != (1.0 if isinstance(step, float) else 1)
        if show_step:
            out.append(f"{number(r.start + step)}..")
        if r.end is not None:
            out.append(f"{'<' if not r.end_inclusive else ''}{number(r.end)}")
        return "".join(out)

    # ---------------------------------------------------------- containers

    def write(self, obj: object, path: str, depth: int = 0) -> str:
        if depth > _MAX_DEPTH:
            raise self._fail(f"value nests more than {_MAX_DEPTH} levels deep", path)
        if isinstance(obj, (list, tuple)):
            return self._list(obj, path, depth)
        if isinstance(obj, dict):
            return self._record(obj, path, depth)
        return self._scalar(obj, path)

    def _element(self, obj: object, path: str, depth: int) -> str:
        """A container element: strings go bare when they survive it
        (value_to_string_without_quotes)."""
        if isinstance(obj, str):
            return self._quote(obj) if needs_quoting(obj) else obj
        return self.write(obj, path, depth)

    def _record(self, record: dict[Any, Any], path: str, depth: int) -> str:
        nl, sep, kv_sep = self._nl, self._sep, self._kv_sep
        comma = "," if self.commas else ""
        idt, idt_in = self._idt(depth), self._idt(depth + 1)
        items: list[str] = []
        for i, (key, value) in enumerate(record.items()):
            if not isinstance(key, str):
                raise self._fail(
                    f"record keys must be strings, not {type(key).__name__}",
                    path,
                )
            name = escape_quote_string(key) if needs_quoting(key) else key
            items.append(
                f"{idt_in}{name}:{kv_sep}"
                f"{self._element(value, f'{path}.{key}', depth + 1)}"
            )
        body = f"{comma}{sep}{nl}".join(items)
        return "{" + f"{nl}{body}{nl}{idt}" + "}"

    def _list(self, values: Iterable[object], path: str, depth: int) -> str:
        values = list(values)
        # a non-empty header from get_columns means every element is a
        # record with exactly these keys, in this order
        rows_as_records = cast(list[dict[Any, Any]], values)
        nl, sep = self._nl, self._sep
        comma = "," if self.commas else ""
        idt, idt_in = self._idt(depth), self._idt(depth + 1)

        headers = _get_columns(values)
        if headers and all(list(v.keys()) == headers for v in rows_as_records):
            if not self.list_of_records:
                return self._table(rows_as_records, headers, path, depth)
            # --list-of-records: each record inline on its own line, even
            # under indentation (raw keeps its raw separators)
            inline_indentation = "" if self.indentation == "" else None
            inline = _Writer(
                inline_indentation, self.raw_strings, True, self.commas
            )
            items = [
                f"{idt_in}{inline.write(v, f'{path}[{i}]', depth + 1)}"
                for i, v in enumerate(rows_as_records)
            ]
            return "[" + f"{nl}{f'{comma}{sep}{nl}'.join(items)}{nl}{idt}" + "]"

        items = [
            f"{idt_in}{self._element(v, f'{path}[{i}]', depth + 1)}"
            for i, v in enumerate(values)
        ]
        return "[" + f"{nl}{f'{comma}{sep}{nl}'.join(items)}{nl}{idt}" + "]"

    def _table(
        self, values: list[dict[Any, Any]], headers: list[str], path: str,
        depth: int,
    ) -> str:
        """The table form. Under indentation the columns are aligned: the
        column width is the maximum display width of the header and every
        cell of that column, and the padding after each cell (comma
        included) fills it to width + 2. The last column is not padded."""
        nl, sep = self._nl, self._sep
        comma = "," if self.commas else ""
        idt, idt_in = self._idt(depth), self._idt(depth + 1)
        idt_in2 = self._idt(depth + 2)

        if self.indentation:  # aligned form
            col_names = [
                escape_quote_string(h) if needs_quoting(h) else h for h in headers
            ]
            rows = [
                [
                    self._element(v[h], f"{path}[{i}].{h}", depth + 2)
                    for h in headers
                ]
                for i, v in enumerate(values)
            ]
            widths = [display_width(c) for c in col_names]
            for row in rows:
                for i, cell in enumerate(row):
                    widths[i] = max(widths[i], display_width(cell))

            def pad(cells: list[str]) -> str:
                parts = []
                for i, cell in enumerate(cells):
                    if i < len(cells) - 1:
                        with_comma = cell + comma
                        parts.append(
                            with_comma + " " * (widths[i] + 2 - display_width(with_comma))
                        )
                    else:
                        parts.append(cell)
                return "".join(parts)

            header_row = f"[{pad(col_names)}];"
            body = f"{comma}{nl}{idt_in}".join(f"[{pad(row)}]" for row in rows)
            return f"[{nl}{idt_in}{header_row}{sep}{nl}{idt_in}{body}{nl}{idt}]"

        # compact / raw form: bracketed rows, one separator sequence
        headers_out = f"{comma}{sep}{nl}{idt_in2}".join(
            escape_quote_string(h) if needs_quoting(h) else h for h in headers
        )
        row_texts = [
            f"{comma}{sep}{nl}{idt_in2}".join(
                self._element(v[h], f"{path}[{i}].{h}", depth + 2)
                for h in headers
            )
            for i, v in enumerate(values)
        ]
        body = f"{nl}{idt_in}]{comma}{sep}{nl}{idt_in}[{nl}{idt_in2}".join(row_texts)
        return (
            f"[{nl}{idt_in}[{nl}{idt_in2}{headers_out}{nl}{idt_in}];{sep}{nl}"
            f"{idt_in}[{nl}{idt_in2}{body}{nl}{idt_in}]{nl}{idt}]"
        )


def _get_columns(values: list[object]) -> list[str]:
    """The table header a list of records would have: every record's keys,
    in first-appearance order (nu-engine get_columns). Empty when any
    element is not a record."""
    columns: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            return []
        for key in value:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return columns
