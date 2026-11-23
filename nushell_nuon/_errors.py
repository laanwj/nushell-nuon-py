"""Exception types for the nuon codec.

Decode errors follow ``json.JSONDecodeError``'s shape: they carry the message,
the offending position in the document, the 1-based line and column, and a
bounded excerpt of the source line for context. Exactly one error is reported
per failed document, in nushell's own reporting order (see ``_parser``).

Encode errors carry the path of the offending value inside the object
(``$.a[2].b``), so a rejection in a nested structure points at the culprit.
"""

from __future__ import annotations


class NuonError(ValueError):
    """Base class for all errors raised by the nuon codec."""


class NuonDecodeError(NuonError):
    """Nuon text that cannot be decoded."""

    def __init__(self, msg: str, doc: str, pos: int) -> None:
        if not 0 <= pos <= len(doc):
            pos = min(max(pos, 0), len(doc))
        line_start = doc.rfind("\n", 0, pos) + 1
        line_end = doc.find("\n", pos)
        if line_end == -1:
            line_end = len(doc)
        lineno = doc.count("\n", 0, pos) + 1
        colno = pos - line_start + 1
        line = doc[line_start:line_end]
        if len(line) > 160:
            # Bound the excerpt like nushell's error window: a window around
            # the error, not the whole (possibly huge) line.
            cut_l = max(line_start, pos - 60)
            cut_r = min(line_end, pos + 60)
            line = doc[cut_l:cut_r]
        errmsg = f"{msg} (line {lineno} column {colno})"
        if line.strip():
            errmsg = f"{errmsg}: {line!r}"
        super().__init__(errmsg)
        self.msg = msg
        self.doc = doc
        self.pos = pos
        self.lineno = lineno
        self.colno = colno


class NuonEncodeError(NuonError):
    """A Python value that cannot be encoded as nuon."""

    def __init__(self, msg: str, path: str = "$") -> None:
        if path == "$":
            super().__init__(msg)
        else:
            super().__init__(f"{msg} (at {path})")
        self.path = path
