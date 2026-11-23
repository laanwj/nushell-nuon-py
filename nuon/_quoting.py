"""String quoting, replicating nushell's ``nu-utils/src/quoting.rs``:
which strings may be written bare, how quoted strings are escaped, and
when the raw ``r#'…'#`` form is used.
"""

from __future__ import annotations

from ._unicode import is_decimal_digit, is_white_space

__all__ = ["as_raw_string", "escape_quote_string", "needs_quoting"]

# The quote-forcing characters: nushell gives these bytes meaning (or the
# reader has to reject them), so a string containing one is always quoted.
_FORCING_CHARS = frozenset("!\"#$'(),.:;=?`[]{}|")

_KEYWORDS = frozenset({"true", "false", "null", "&&"})


def _is_float_name(s: str) -> bool:
    """A whole-string float keyword: an optional sign and `inf`, `infinity`
    or `nan` in any ASCII casing. It would read back as a float, so it is
    quoted even though the reader now only accepts exactly `inf`, `nan` and
    `NaN`."""
    body = s[1:] if s[:1] in "+-" else s
    return (
        bool(body)
        and body.isascii()
        and body.isalpha()
        and body.lower() in ("inf", "infinity", "nan")
    )


def needs_quoting(s: str) -> bool:
    """Whether `s` must be written quoted (needs_quoting)."""
    if not s:
        return True
    if s in _KEYWORDS:
        return True
    if _is_float_name(s):
        return True
    for c in s:
        if (
            c in _FORCING_CHARS
            or is_white_space(c)
            or is_decimal_digit(c)
            or c == "\0"
        ):
            return True
    return False


def escape_quote_string(s: str) -> str:
    """A double-quoted form of `s`: only `"`, `\\` and NUL are escaped
    (a raw NUL would make the document unreadable); everything else,
    newlines included, is written verbatim inside the quotes."""
    out = ['"']
    for c in s:
        if c == '"' or c == "\\":
            out.append("\\" + c)
        elif c == "\0":
            out.append("\\0")
        else:
            out.append(c)
    out.append('"')
    return "".join(out)


def as_raw_string(s: str) -> str | None:
    """The `r#'…'#` form of `s`, or None when it would not help.

    Only a string containing `"` or `\\` benefits; a NUL cannot be
    represented in a raw string at all. The hash run grows until it is
    longer than any hash run the body starts with and the closing
    `'` + hashes does not occur inside the body.
    """
    if "\0" in s or ('"' not in s and "\\" not in s):
        return None
    hashes = 1
    while True:
        closing = "'" + "#" * hashes
        if not s.startswith("#" * hashes) and closing not in s:
            return f"r{'#' * hashes}'{s}'{'#' * hashes}"
        hashes += 1
