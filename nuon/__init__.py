"""NUON (Nushell Object Notation) codec, matching nushell's Rust
implementation (``from nuon`` / ``to nuon``).

The interface mirrors the standard ``json`` module::

    nuon.dumps(value) -> str      nuon.dump(value, fp)
    nuon.loads(text) -> value     nuon.load(fp)

See the README for the value mapping and the behavioral notes.
"""

from __future__ import annotations

from ._errors import NuonDecodeError, NuonEncodeError, NuonError
from ._parser import load, loads
from ._types import CellPath, Datetime, Duration, Filesize, PathMember, Range
from ._writer import dump, dumps

__version__ = "0.1.0"

__all__ = [
    "CellPath",
    "Datetime",
    "Duration",
    "Filesize",
    "NuonDecodeError",
    "NuonEncodeError",
    "NuonError",
    "PathMember",
    "Range",
    "__version__",
    "dump",
    "dumps",
    "load",
    "loads",
]
