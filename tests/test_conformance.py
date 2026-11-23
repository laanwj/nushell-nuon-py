"""Differential conformance against a real nushell binary.

This suite compares every reader and writer behavior with the actual
implementation (``from nuon`` / ``to nuon``) rather than trusting the port.
It is skipped entirely when no ``nu`` binary is found; to build the
reference in a nushell checkout:

    CARGO_TARGET_DIR=/tmp/nu-target cargo build -p nu

The binary is located via ``$NU_BIN``, then ``/tmp/nu-target/debug/nu``,
then the ``PATH``.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import tempfile
from datetime import timedelta, timezone

import pytest

import nuon
from nuon import Datetime, Duration, Filesize, Range, loads

# ------------------------------------------------------------------ plumbing


def _find_nu() -> str | None:
    candidates = [
        os.environ.get("NU_BIN"),
        "/tmp/nu-target/debug/nu",
        shutil.which("nu"),
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


NU = _find_nu()

pytestmark = pytest.mark.skipif(NU is None, reason="no nushell binary found")


def nu_pipeline(text: str, pipeline: str) -> tuple[str, int]:
    """Feed nuon text through a nu pipeline; returns (stdout, returncode).

    Output is captured as bytes and decoded: nushell writes `\r` and other
    control bytes verbatim, and text-mode capture would translate them.
    """
    fd, path = tempfile.mkstemp(suffix=".nuon", dir="/tmp")
    with os.fdopen(fd, "w", newline="") as f:
        f.write(text)
    try:
        env = dict(os.environ, HOME=tempfile.mkdtemp(prefix="nu-conf-"))
        result = subprocess.run(
            [NU, "-n", "-c", f"open --raw {path!r} | {pipeline}"],  # type: ignore[list-item]
            capture_output=True, timeout=30, env=env,
        )
        return result.stdout.decode("utf-8").rstrip("\n"), result.returncode
    finally:
        os.unlink(path)


def py_loads(text: str) -> tuple[bool, object]:
    try:
        return True, loads(text)
    except nuon.NuonDecodeError:
        return False, None


# --------------------------------------------------------------- the corpora

# Every literal family and edge pinned during development, the nuon
# specification's examples (adjusted where nushell moved on), and the nuon
# crate's own tests.
READER_TEXTS = [
    # scalars
    "null", "true", "false", "1", "-1", "+5", "-0", "007", "1_000", "_1",
    "0xff", "0o17", "0b101", "-0x10", "+0xff", "0xAB", "0x_f_f",
    "0xFFFFFFFFFFFFFFFF", "0x8000000000000000",
    "1.5", "1.", ".5", "1e3", "1E-3", "1e400", "1e-999", "-0.0",
    "nan", "NaN", "+nan", "-nan", "inf", "-inf",
    '"hi"', "'hi'", "[r#'x'#]", "[r##'it'#s'##]", "[r#'path\\to\\file'#]",
    # strings
    "[abc, xyz]", "[a-b, -, --, a/b, a\\b, &, %, @, ^, ~, +, *, <, >, café]",
    "[a#b]", "[a, # note\n b]", "[voilà#tag]", "['a\\tb', `a b`]",
    "['a''b']", '["a""b"]', "[r'a' b]", "[a'b c'd]", '["a" "b"]',
    '["a"b]', "['a'\"b\"]",
    '["a\\u{41}b"]', '["\\u{041}"]', '["\\xC3\\xA9", "\\x41"]',
    '["a\\(b", "a\\#b", "a\\~b", "a\\ab", "a\\ b"]',
    '["\\n\\t\\r\\b\\f\\e\\0"]',
    # not-quite keywords and numbers stay strings
    "[NAN, Inf, INF, Infinity, -infinity, nAn, info, İNF]",
    "[1z, 1s, 1m, 1h, 1d, 1week, 1second, 2000-1-1, 2023-02-29.log]",
    "[a(b)]",
    # durations
    "[1ns, 1us, 1µs, 1μs, 1ms, 1sec, 1min, 1hr, 1day, 1wk]",
    "[1.5min, -1_000ns, +5sec, 1.5ns, 1z]", "[0eb, 0zb]",
    # filesizes
    "[1KB, 1kB, 1kiB, 1B, 1_000_000b, 100kib]", "[-1kb, -1kib]",
    # datetimes
    "2000-01-01", "2000-01-01T12:34:56+05:30", "2000-01-01t12:34:56+05:30",
    "2000-01-01T12:34:56Z", "2000-01-01T12:34:56z", "2000-01-01T12:34:56",
    "2000-01-01T12:34:56.123+00:00", "2000-01-01T12:34:56.123456789+00:00",
    "2000-01-01T12:34:56.1234567891234+00:00", "2000-01-01T12:34:56.1+05:30",
    "[2000-01-01T23:59:60Z]", "[2000-01-01T12:30:60Z]",
    "[2023-02-29, 2000-13-45, 2000-04-31, 2100-02-29]",
    "[2000-13-45T12]", "[2000-01-01T12:34:56+99:99]",
    "[2000-01-01T12:34:56.123+05:30, 0001-01-01]",
    # binary
    "[0x[be ef], 0x[beef], 0x[BE, EF], 0x[b], 0b[1010], 0x[], 0o[17], 0o[377]]",
    "[0x[be,]]", "[0b[1 0 10]]", "[0x[be # comment\nef]]",
    # ranges
    "[1..5, 1..<5, 1.., ..5, ..=5, 1..3..9, 5..1, -5..5, 1.5..2.5]",
    "[1.0..2.0, 1.0..1.5..2.0, 1..1..5]",
    # cell paths
    "[$.a.b, $.0, $.a.0.b, $.'a b', $.a?, $.a!, $.a!?, $.a?!, $., $.0?]",
    "[$.'a b', $.r#'a'#]",
    # containers
    "[1, 2, 3]", "[1 2 3]", "[1, 2, 3,]", "[1,\n2,]", "[[1, 2], [3]]",
    "[[1 2 3], 4]", "{a: 1 b: 2}", "{a:1,b:[1,2]}", "{a: {}}",
    "{true: 1, null: 2, 1: 3, 2000-01-01: 4}", "{...a: 1}",
    '{k: "a:b", u: "http://x.com"}',
    "{a: 1, a: 2}",
    # table form
    "[[a, b]; [1, 2], [3, 4]]", "[[a b]; [1 2] [3 4]]", "[[1 2]; [3 4]]",
    "[[a]; [[[b]; [1]]]]", "[[a]; [{x: 1}]]", "[[a b]; [[c]; [1]]]",
    "[[a, b, \"c d\"]; [\"\", la], [le, lu]]",
    "[[my, columns]; [abc, xyz], [def, ijk]]",
    "[[repeated repeated]; [abc, xyz]]",
    # document level
    "", "  \n", "# only a comment", "[1] # trailing comment", "# leading\n[1]",
    "[1] o> /tmp/nu-conformance-ignored", "| [1]", "[1 \n| 2]",
    "-abc", "-5", "+5", "-0xff", "-(1)", "-$x", "1 + 2", "[1] [2]",
    "[1] | [2]", "[1]\n[2]", "[1]; [2]", "[1] |", "[a && b]", "[a || b]",
    "[1 2]]", "[a; b]", "abc", "abc def", "hi", "`hi`", "not", "if", "match",
    "1 = 2", "[1 + 2]", "[1 .. 5]", "[a | b | c]", "[| a]", "[= 1]",
    "[o> x]", "[...]", "[...abc]", "[...[1]]", "{...{a: 1}}", "[1....5]",
    "[...]", "{...}", "[[a]; [...x]]",
    # escapes and refusals
    '["\\u0041"]', '["\\u{110000}"]', '["\\u{D800}"]', '["\\u{0000041}"]',
    '["\\xff"]', '["\\xC3"]', '["\\x4"]', '["\\q"]', '["\\"]', '["a\nb"]',
    "[a, 'a b', a1, \"0x\", x-1, a-b, a_b, E5, 1s, 3/4]",
    # the nushell sample fixture (comments, missing commas, whitespace)
    """# Some sample nuon values
[
    # The nuon compact table format
    [[a, nuon, table]; [1, 2, 3], [4, 5, 6]],

    # A filesize
    100kib,

    # A duration
    100sec

    # A boolean
    true,

    # A record
    {name: "Bobby", age: 99}

    # Binary data
    0x[11, ff, ee, 1f]
]""",
    # BOM handling
    "﻿[1, 2]",
]

# texts whose reading must fail in both implementations
READER_ERROR_TEXTS = [
    "[0o8]", "[0x]", "[0xg]", "[0xdump]", "[0xdump..5]",
    "[9223372036854775808]", "[0x10000000000000000]",
    "[2023-02-29]", "[2000-13-45]",
    "[0xns]", "[1zb]", "[1mins]", "[8EiB]", "[9223372036854775808ns]",
    "[1e30ns]", "[1e30sec]", "[0o[777]]", "[1..1..5]", "[99999999999999999999..5]",
    "{a: 1, a: 2}", "[[a b a]; [1 2 3]]", "[[a b];]", "[[a b]; [1]]",
    "[[a b]; [1 2 3]]", "[[a b]; [[c]; [1]]]", "[[a]; [...[1]]]",
    "[1, 2", "{a: 1", '["abc', "[0x[be", "r#'abc", "[a(b)]", "[$x]",
    "{k: a:b}", "{k: http://x.com}", "{k: 1:2}",
    "[...[1]]", "{...{a: 1}}", "[[c d]; [1]]",
]

WRITER_TEXTS = [
    "[1.0, 1e-5, 1e15, 1e16, 1e-30, -0.0, 0.1, 1e-7, 1e-9, 1.5e-10, 123.456]",
    "[inf, -inf, nan, NaN]",
    "[1sec, 1.5min, -1000ns, 1hr, 1.5ns]",
    "[1kb, -1kb, 1048576b, 0b, .5b]",
    "[0x[deadbeef], 0x[], 0x[0b]]",
    "[2000-01-01]",
    "[2000-01-01T12:34:56.123+05:30]",
    "[2000-01-01T12:34:56.123456+05:30]",
    "[2000-01-01T12:34:56.123456789+05:30]",
    "[2000-01-01T12:34:56.1+05:30]",
    "[2000-01-01T23:59:60Z]",
    "[2000-01-01T23:59:60.5Z]",
    "[$.a.b, $.0, $.'a b', $.a?, $.a!, $.a!?, $., $.0?]",
    "[1..5, 1..<5, 1.., ..5, ..=5, 1..3..9, 5..1, 1.5..2.5, 1.0..2.0, 1.0..1.5..2.0]",
    "[a, 'a b', a1, \"0x\", x-1, a-b, a_b, café, a​b]",
    '["true", "false", "null", "NaN", "NAN", "nan", "+nan", "-nan", "inf", '
    '"+inf", "-inf", "INF", "Infinity", "+Infinity", "-Infinity", "INFINITY", '
    '"+19.99", "-19.99", "19.99b", "19.99kb", "19.99mb", "19.99gb", '
    '"19.99tb", "19.99pb", "19.99eb", "19.99zb", "19.99kib", "19.99mib", '
    '"19.99gib", "19.99tib", "19.99pib", "19.99eib", "19.99zib", "19ns", '
    '"19us", "19ms", "19sec", "19min", "19hr", "19day", "19wk", '
    '"-11.0..-15.0", "11.0..-15.0", "-11.0..15.0", "-11.0..<-15.0", '
    '"11.0..<-15.0", "-11.0..<15.0", "-11.0..", "11.0..", "..15.0", '
    '"..-15.0", "..<15.0", "..<-15.0", "2000-01-01", "2022-02-02T14:30:00", '
    '"2022-02-02T14:30:00+05:00", ", ", "", "&&"]',
    '["hello\\"world"]', '["hello\\\\world"]', "[hello\\\\world]",
    '{s: "hello\\"world"}', '[""]', '"hello"',
    "[[a b]; [1 2] [3 4]]",
    "[[name age]; [Alice 30] [Bob 25]]",
    "[[name age active]; [alice 22 true] [bob 20 false] [charlie 20 false]]",
    '[[a b]; ["日本" 1] ["abcde" 2]]',
    '[[a b]; ["…" 1] ["abcde" 2]]',
    '[[a b]; ["🎉" 1] ["abcde" 2]]',
    '[[a b]; ["👨‍👩‍👦" 1] ["abcde" 2]]',
    '[[a b]; ["🇩🇪" 1] ["abcde" 2]]',
    '[[a b]; ["✌️" 1] ["abcde" 2]]',
    '[[a b]; ["1️⃣" 1] ["abcde" 2]]',
    '[[a b]; ["á" 1] ["abcde" 2]]',
    "[[a]; [{x: 1}]]",
    "[[a]; [[[b]; [1]]]]",
    '[[a, b]; ["", la], [le, lu]]',
    "{a: 1, b: [1, 2]}",
    "{a: {}}",
    "[[], {}]",
    "[[{a: 1, b: 2}, {b: 3, a: 4}]]",
    "[{}, {}]",
    "[[]]",
    "{a: 1}",
    "[a, b]",
    "[[1, 2], [3, 4]]",
    "'hello \"world\"'",
    "'hello world'",
    "'#\"foo'", "'##\"foo'", "'a\\0\"b'",
    "[0.1, 0.2, 3.141592653589793, 1e-300]",
    '[["", " "], ["a​b", "\0"]]',
]

FLAG_MATRIX: list[tuple[str, dict]] = [
    ("", {}),
    ("--raw", dict(raw=True)),
    ("--pretty", dict(pretty=True)),
    ("--indent 2", dict(indent=2)),
    ("--indent 3", dict(indent=3)),
    ("--indent 4", dict(indent=4)),
    ("--tabs 1", dict(tabs=1)),
    ("--tabs 2", dict(tabs=2)),
    ("--indent 0", dict(indent=0)),
    ("--no-commas", dict(commas=False)),
    ("--no-commas --pretty", dict(commas=False, pretty=True)),
    ("--raw-strings", dict(raw_strings=True)),
    ("--raw-strings --pretty", dict(raw_strings=True, pretty=True)),
    ("--list-of-records", dict(list_of_records=True)),
    ("--list-of-records --indent 2", dict(list_of_records=True, indent=2)),
    ("--raw --list-of-records", dict(raw=True, list_of_records=True)),
    ("--pretty --raw", dict(pretty=True, raw=True)),
    ("--indent 4 --tabs 1", dict(indent=4, tabs=1)),
]


# ------------------------------------------------------------------- readers


@pytest.mark.parametrize("text", READER_TEXTS)
def test_reader_matches_nushell(text):
    """Both implementations agree on whether the text reads, and on the
    value it reads (compared through each side's canonical raw form)."""
    nu_out, nu_rc = nu_pipeline(text, "from nuon | to nuon --raw")
    py_ok, value = py_loads(text)
    nu_ok = nu_rc == 0
    assert py_ok == nu_ok, f"classification differs: {text!r}"
    if not nu_ok:
        return
    canonical = nuon.dumps(value, raw=True)
    assert canonical == nu_out, f"value differs: {text!r}"


@pytest.mark.parametrize("text", READER_ERROR_TEXTS)
def test_reader_rejects_like_nushell(text):
    _, nu_rc = nu_pipeline(text, "from nuon | to nuon --raw")
    py_ok, _ = py_loads(text)
    assert py_ok == (nu_rc == 0), f"classification differs: {text!r}"


@pytest.mark.parametrize("text", READER_TEXTS)
def test_reader_type_matches_nushell(text):
    """The ``describe`` type agrees, catching misparses the canonical form
    could hide (int vs float, string vs unit literal, ...)."""
    nu_out, nu_rc = nu_pipeline(text, "from nuon | describe -d | get type")
    py_ok, _ = py_loads(text)
    assert py_ok == (nu_rc == 0), f"classification differs: {text!r}"
    if not py_ok:
        return
    py_type = {
        type(None): "nothing", bool: "bool", int: "int", float: "float",
        str: "string", bytes: "binary", list: "list", dict: "record",
        Duration: "duration", Filesize: "filesize", Datetime: "datetime",
        Range: "range",
    }.get(type(loads(text)))
    if py_type is None:  # a CellPath
        py_type = "cell-path"
    assert py_out_equal(nu_out, py_type), f"type differs: {text!r}"


def py_out_equal(nu_type: str, py_type: str | None) -> bool:
    # nu describes tables as `table<>` and their element type as `list<any>`
    if py_type == "list" and nu_type.startswith(("table", "list")):
        return True
    return nu_type == py_type


# ------------------------------------------------------------------- writers


@pytest.mark.parametrize("text", WRITER_TEXTS)
@pytest.mark.parametrize(
    ["flags", "kwargs"], FLAG_MATRIX, ids=[f for f, _ in FLAG_MATRIX]
)
def test_writer_is_byte_identical(text, flags, kwargs):
    nu_out, nu_rc = nu_pipeline(text, f"from nuon | to nuon {flags}")
    assert nu_rc == 0, f"nushell rejected the corpus text: {text!r}"
    value = loads(text)
    assert nuon.dumps(value, **kwargs) == nu_out, f"{text!r} {flags}"


def test_raw_with_no_commas_is_rejected_by_both():
    _, nu_rc = nu_pipeline("[1]", "from nuon | to nuon --raw --no-commas")
    assert nu_rc != 0
    with pytest.raises(ValueError):
        nuon.dumps([1], raw=True, commas=False)


# --------------------------------------------------------------------- fuzz


def _random_string(rng: random.Random) -> str:
    alphabet = [
        "abcXYZ", "0123", "-+._", " \t\n", ":", ",;[]{}()|", "#'\"`$",
        "?!*<>=&/~^%", "é日かก…", "\0​‍﻿́️",
        "🎉🇩🇪👨‍👩‍👦✌️1️⃣", "½Ⅴ③௰０٣", "‌‍",
    ]
    pool = "".join(alphabet[: rng.randint(1, len(alphabet))])
    return "".join(rng.choice(pool) for _ in range(rng.randint(0, 12)))


def _random_value(rng: random.Random, depth: int = 0) -> object:
    kind = rng.randint(0, 13 if depth < 3 else 9)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.choice([
            rng.randint(-(2**63), 2**63 - 1), 0, 1, -1,
            2**63 - 1, -(2**63), rng.randint(-1000, 1000),
        ])
    if kind == 3:
        return rng.choice([
            rng.uniform(-1e6, 1e6), 0.1, -0.0, 1e-7, 1e15, 1e16,
            float("inf"), float("-inf"), float("nan"),
            rng.uniform(-1, 1), 123.456,
        ])
    if kind == 4:
        return _random_string(rng)
    if kind == 5:
        return bytes(rng.randint(0, 255) for _ in range(rng.randint(0, 8)))
    if kind == 6:
        return Duration(rng.randint(-(2**63), 2**63 - 1))
    if kind == 7:
        return Filesize(rng.randint(-(2**63), 2**63 - 1))
    if kind == 8:
        off = rng.randint(-23 * 60, 23 * 60)
        tz = timezone(timedelta(minutes=off))
        return Datetime(
            rng.randint(1, 9999), rng.randint(1, 12), rng.randint(1, 28),
            rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59),
            rng.randint(0, 999999), tz, nanosecond=rng.randint(0, 999),
        )
    if kind == 9:
        from nuon import CellPath, PathMember

        members = []
        for _ in range(rng.randint(0, 4)):
            if rng.random() < 0.4:
                members.append(PathMember(rng.randint(0, 50),
                                          optional=rng.random() < 0.3))
            else:
                members.append(PathMember(
                    _random_string(rng),
                    optional=rng.random() < 0.3,
                    insensitive=rng.random() < 0.3,
                ))
        return CellPath(members)
    if kind == 10:
        start = rng.randint(-100, 100)
        step = rng.choice([1, -1, 2, -3, 7])
        end = start + step * rng.randint(0, 20)
        if rng.random() < 0.3:
            end = None
        return Range(start, step, end, end_inclusive=rng.random() < 0.5)
    if kind == 11:
        return Range(
            round(rng.uniform(-50, 50), 3), round(rng.uniform(0.1, 3), 3),
            round(rng.uniform(50, 100), 3), end_inclusive=rng.random() < 0.5,
        )
    if kind == 12:
        return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 5))]
    return {
        _random_string(rng) or "k": _random_value(rng, depth + 1)
        for _ in range(rng.randint(0, 5))
    }


def _values_equal(a: object, b: object) -> bool:
    """== with NaN treated as equal to itself. A float range compares by its
    rendered form: its step round-trips through the middle element, and the
    subtraction that recovers it is not exact (in nushell either)."""
    if isinstance(a, Range) and isinstance(b, Range):
        return a.start == b.start and a.end == b.end and (
            a.end_inclusive == b.end_inclusive
        ) and (a.step == b.step or a.start + a.step == b.start + b.step)
    if isinstance(a, float) and isinstance(b, float):
        if a != a and b != b:
            return True
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(
            _values_equal(x, y) for x, y in zip(a, b)
        )
    if isinstance(a, dict) and isinstance(b, dict):
        return list(a) == list(b) and all(
            _values_equal(a[k], b[k]) for k in a
        )
    return type(a) is type(b) and a == b


def test_value_round_trip_fuzz():
    """Random values: our writer's output reads back to the same value in
    both implementations, and nushell's canonical form agrees with ours."""
    rng = random.Random(20260923)
    for i in range(60):
        value = _random_value(rng)
        text = nuon.dumps(value)
        py_back = loads(text)
        assert _values_equal(py_back, value), f"case {i}: {text!r}"
        nu_out, nu_rc = nu_pipeline(text, "from nuon | to nuon --raw")
        assert nu_rc == 0, f"nushell rejected our output: {text!r}"
        assert nu_out == nuon.dumps(loads(text), raw=True), (
            f"case {i}: canonical forms differ: {text!r}"
        )


def test_reader_mutation_fuzz():
    """Mutations of the corpus: both implementations must agree on whether
    the result still reads. This is the strictness check — a silent
    acceptance or a silent rejection anywhere shows up here."""
    rng = random.Random(20261123)
    mutations = []
    source = READER_TEXTS + WRITER_TEXTS
    for _ in range(200):
        text = list(rng.choice(source))
        for _ in range(rng.randint(1, 3)):
            op = rng.randint(0, 2)
            if not text:
                break
            pos = rng.randrange(len(text))
            if op == 0:
                del text[pos]
            elif op == 1:
                text.insert(pos, rng.choice("[]{}():;,'\"`#$?.!|&= \n\t0ax1e-_"))
            else:
                text[pos] = rng.choice("[]{}():;,'\"`#$?.!|&= \n\t0ax1e-_/")
        mutations.append("".join(text))
    for text in mutations:
        _, nu_rc = nu_pipeline(text, "from nuon | to nuon --raw")
        py_ok, _ = py_loads(text)
        assert py_ok == (nu_rc == 0), f"classification differs: {text!r}"


def test_display_width_fuzz():
    """Random unicode strings as table cells: the aligned padding our
    display_width computes matches nushell's exactly."""
    rng = random.Random(20261124)
    pools = [
        "abcde", "日本語が", "🎉🚀✨", "👨‍👩‍👦🇩🇪✌️1️⃣", "áé ̀́",
        "​‍­", "0123½Ⅴ③௰０٣", " \t\n", "—–…§¶", "กำ", "אב", "≡░▒▓",
    ]
    for _ in range(80):
        cell = "".join(
            rng.choice(rng.choice(pools)) for _ in range(rng.randint(1, 6))
        )
        if "\n" in cell:
            continue  # multi-line cells are covered by the corpus
        rows = [{"a": cell, "b": 1}, {"a": "abcde", "b": 2}]
        nu_out, nu_rc = nu_pipeline(
            nuon.dumps(rows), "from nuon | to nuon --pretty"
        )
        assert nu_rc == 0
        assert nuon.dumps(loads(nuon.dumps(rows)), pretty=True) == nu_out, (
            f"width differs for cell {cell!r}"
        )
