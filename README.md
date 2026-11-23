# nuon — NUON for Python

A pure-Python, dependency-free codec for [NUON][nuon-book], the data
notation built into [nushell][nushell]. The interface mirrors the standard
`json` module; the behavior mirrors nushell's own `from nuon` / `to nuon`
byte for byte.

It is developed against **nushell 0.117.1** and tracks the *implementation*
where it has moved on from the [formal specification][spec] in the nushell
tree (see [Divergences](#divergences-from-the-specification) below).

[nuon-book]: https://www.nushell.sh/book/quick_tour.html#nuon
[nushell]: https://www.nushell.sh
[spec]: https://github.com/nushell/nushell/blob/main/crates/nuon/spec/nuon_formal_specification.md

## Install

Python 3.10+; no dependencies.

    pip install .

## Usage

```python
>>> import nuon
>>> nuon.dumps([1, 2, 3])
'[1, 2, 3]'
>>> nuon.dumps({"name": "Alice", "age": 30}, pretty=True)
'{\n  name: Alice,\n  age: 30\n}'
>>> nuon.loads("[[name age]; [Alice 30], [Bob 25]]")
[{'name': 'Alice', 'age': 30}, {'name': 'Bob', 'age': 25}]
>>> nuon.dumps(nuon.loads("1.5min"))
'90000000000ns'
```

### API

`nuon.loads(s)` / `nuon.load(fp)`
:   Deserialize nuon text (a `str`, or UTF-8 `bytes`/`bytearray`) from a
    string or a readable file-like object.

`nuon.dumps(obj, *, indent=None, tabs=None, pretty=False, raw=False, raw_strings=False, list_of_records=False, commas=True)` / `nuon.dump(obj, fp, **kw)`
:   Serialize to nuon text. The keyword flags mirror `to nuon`'s flags
    exactly, including their precedence:

    | keyword | `to nuon` flag | effect |
    | --- | --- | --- |
    | `raw` | `--raw` / `-r` | no whitespace at all; wins over every indent form |
    | `tabs=N` | `--tabs N` / `-t` | N tabs per level; wins over `indent` |
    | `indent=N` | `--indent N` / `-i` | N spaces per level; `0` means `raw` |
    | `pretty` | `--pretty` / `-p` | the same as `indent=2` |
    | `commas=False` | `--no-commas` / `-c` | separate items with whitespace |
    | `raw_strings=True` | `--raw-strings` / `-R` | `r#'…'#` for strings containing `"` or `\` |
    | `list_of_records=True` | `--list-of-records` / `-l` | never use the table form |

    `raw=True, commas=False` raises `ValueError` — the two contradict each
    other, exactly as nushell rejects `--raw --no-commas`.

### Value mapping

| nuon | Python |
| --- | --- |
| `null` | `None` |
| `true` / `false` | `bool` |
| `int` (signed 64-bit) | `int` — out-of-range values raise rather than lose precision |
| `float` | `float` |
| `string` | `str` |
| `list` | `list` |
| `record` | `dict` — insertion order is part of the value; duplicate keys are an error |
| `binary` | `bytes` (dumps also accepts `bytearray`) |
| `duration` | `nuon.Duration` — a `timedelta` subclass carrying the sub-microsecond remainder in `.nanosecond`, constructed from the nanosecond count; `.total_ns` gives it back exactly |
| `filesize` | `nuon.Filesize` — an `int` subclass counting bytes |
| `datetime` | `nuon.Datetime` — a `datetime` subclass with `.nanosecond` (0-999) and `.leap_second` (the `:60` spelling, which `datetime` cannot hold) |
| `cell-path` | `nuon.CellPath` — of `nuon.PathMember(value, optional, insensitive)` |
| `range` | `nuon.Range(start, step, end, end_inclusive)` — `end=None` is unbounded |

`dumps` also accepts plain `timedelta` (microsecond precision) and plain
`datetime` values: a naive `datetime` is written as UTC, which is what
nushell's reader assumes for offset-less datetimes.

Arithmetic on `Duration`/`Datetime` returns the plain base type and drops
the nanosecond/leap-second extras — that is inherited `datetime` behavior,
not a silent data change in the codec.

### The table form

A non-empty list whose elements are all records with the same keys in the
same order is written the way `to nuon` writes it — as a table with aligned
columns:

```python
>>> print(nuon.dumps(
...     [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}], pretty=True))
[
  [name,  age];
  [Alice, 30],
  [Bob,   25]
]
```

Column alignment uses terminal display width (CJK characters count 2, a
ZWJ-joined family emoji or a flag counts as one wide cell, combining marks
count 0), replicating the `unicode-width` crate as nushell uses it.

## Strictness

Errors are loud and precise. `loads` never repairs, coerces or reinterprets
input:

- invalid UTF-8, a stray `]`, an unclosed string, an extra token after a
  closing quote, an over-long integer, an invalid radix literal (`0o8`),
  an impossible date (`2023-02-29`), an overflowed duration, a duplicate
  record key or table column, a semicolon inside a list — all raise
  `nuon.NuonDecodeError` with the line, column and a source excerpt.
- `dumps` raises `NuonEncodeError` (with the path of the offending value,
  like `$.a[1].b`) or `TypeError` for anything it cannot represent exactly:
  ints outside 64 bits, non-string record keys, unknown types.

## Divergences from the specification

The [formal specification][spec] in the nushell tree describes nushell
0.115.1; this module follows the *code* of 0.117.1 wherever the two
disagree, because that is what it interoperates with. The significant
differences, in both directions:

- nushell 0.117 changed several behaviors on purpose (its nu-conformance
  review): signed radix literals (`-0x10` → `-16`), a decimal int outside
  64 bits is an error instead of a float, the only float spellings are
  `inf`, `nan` and `NaN` (case-sensitive; `Infinity` is a string), a token
  with exact date shape but invalid fields is an error, table headers
  coerce their cells to strings, `+5sec` is a duration. All reproduced.
- nushell still has open bugs the spec documents as fixed-in-spirit; this
  module reproduces the *current* behavior because that is what files in
  the wild contain: the empty document is `null`, an empty container under
  indentation emits a blank line, filesizes may be negative, a raw NUL is
  accepted inside strings when reading (but always escaped when writing).
- Python `datetime` cannot represent year 0 (nushell/chrono can) or more
  than microsecond precision; `Datetime` carries the nanoseconds, but year
  0 raises a `NuonDecodeError` explaining the limit rather than silently
  shifting the value.
- Error *messages* are this module's own; which inputs error, and what they
  decode to, match nushell exactly (that is what the conformance suite
  checks).
- Nesting is capped at 256 levels (a resource limit the spec sanctions;
  nushell's parser has no cap and dies on its native stack instead).

## Development

    python -m pytest          # unit tests, fast, no nushell needed
    mypy nuon                 # strict, clean

The differential conformance suite (`tests/test_conformance.py`) compares
every behavior against a real nushell binary and skips itself when none is
found. To build the reference in a nushell checkout:

    CARGO_TARGET_DIR=/tmp/nu-target cargo build -p nu

It then runs automatically with `pytest` (located via `$NU_BIN`,
`/tmp/nu-target/debug/nu` or the `PATH`), covering:

- reader classification and canonical value for every literal family and
  edge case, plus `describe` type parity,
- writer byte-identity across the full flag matrix,
- agreement on which inputs are rejected,
- seeded fuzzing: random values (all types, nested) round-trip through both
  implementations; mutated corpus texts must classify identically; random
  Unicode table cells must align identically.

## Provenance

Reader and writer semantics were extracted from the nushell sources at
0.117.1 — `crates/nuon/src/{from,to}.rs`, `crates/nu-parser/src/`
(`lex.rs`, `parse_literals.rs`, `parse_expressions.rs`),
`crates/nu-utils/src/{quoting,float}.rs` — and every rule was probed
against the built binary rather than assumed. The Unicode tables (White_Space,
Emoji, Extended_Pictographic) are generated from the Unicode 15.1 UCD.

License: MIT, like nushell itself.
