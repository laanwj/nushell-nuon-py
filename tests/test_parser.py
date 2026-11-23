import math
from datetime import timedelta, timezone

import pytest

from nuon import (
    CellPath,
    Datetime,
    Duration,
    Filesize,
    NuonDecodeError,
    PathMember,
    Range,
    loads,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # scalars
        ("null", None),
        ("true", True),
        ("false", False),
        ("1", 1),
        ("+5", 5),
        ("-0", 0),
        ("007", 7),
        ("1_000", 1000),
        ("_1", 1),  # underscores are stripped anywhere, leading included
        ("0xff", 255),
        ("0o17", 15),
        ("0b101", 5),
        ("-0x10", -16),
        ("+0xff", 255),
        ("0xFFFFFFFFFFFFFFFF", -1),
        ("0x8000000000000000", -(2**63)),
        ("1.5", 1.5),
        ("1.", 1.0),
        (".5", 0.5),
        ("1e3", 1000.0),
        ("1E-3", 0.001),
        ("1e400", float("inf")),
        ("1e-999", 0.0),
        ("-0.0", -0.0),
        ('"hi"', "hi"),
        ("'hi'", "hi"),
        ("[r#'x'#]", ["x"]),
        ("[r##'it'#s'##]", ["it'#s"]),
        # strings in lists: bare words, keywords as strings
        ("[abc]", ["abc"]),
        ("[true]", [True]),
        ("[a-b, -, --, a/b, a\\b, &, %, @, ^, ~, +, *, <, >, café]",
         ["a-b", "-", "--", "a/b", "a\\b", "&", "%", "@", "^", "~", "+", "*", "<", ">", "café"]),
        ("[a#b]", ["a#b"]),
        ("[a, # note\n b]", ["a", "b"]),
        ("[voilà#tag]", ["voilà#tag"]),
        ("[1s, 1m, 1h, 1d, 1week, 1z, 1s, info, İNF]",
         ["1s", "1m", "1h", "1d", "1week", "1z", "1s", "info", "İNF"]),
        ("[NAN, Inf, INF, Infinity, -infinity, nAn]",
         ["NAN", "Inf", "INF", "Infinity", "-infinity", "nAn"]),
        ("['a\\tb', `a b`]", ["a\\tb", "a b"]),
        ("['a''b']", ["a''b"]),
        ('["a""b"]', ['a""b']),
        ("[r'a' b]", ["r'a'", "b"]),
        # durations
        ("[1ns, 1us, 1µs, 1μs, 1ms, 1sec, 1min, 1hr, 1day, 1wk]",
         [Duration(1), Duration(1000), Duration(1000), Duration(1000),
          Duration(10**6), Duration(10**9), Duration(60 * 10**9),
          Duration(3600 * 10**9), Duration(86400 * 10**9), Duration(604800 * 10**9)]),
        ("[1.5min]", [Duration(90 * 10**9)]),
        ("[-1_000ns]", [Duration(-1000)]),
        ("[+5sec]", [Duration(5 * 10**9)]),
        ("[1.5ns]", [Duration(1)]),  # the fraction is dropped below 1µs
        # filesizes
        ("[1KB, 1kB, 1kiB, 1B, 1_000_000b]",
         [Filesize(1000), Filesize(1000), Filesize(1024), Filesize(1), Filesize(10**6)]),
        ("[-1kb]", [Filesize(-1000)]),  # bug 9 still open upstream
        ("[.5b]", [Filesize(0)]),
        ("[0eb]", [Filesize(0)]),
        # datetimes
        ("2000-01-01", Datetime(2000, 1, 1, tzinfo=timezone.utc)),
        ("2000-01-01T12:34:56+05:30",
         Datetime(2000, 1, 1, 12, 34, 56,
                  tzinfo=timezone(timedelta(hours=5, minutes=30)))),
        ("2000-01-01t12:34:56z", Datetime(2000, 1, 1, 12, 34, 56, tzinfo=timezone.utc)),
        ("2000-01-01T12:34:56", Datetime(2000, 1, 1, 12, 34, 56, tzinfo=timezone.utc)),
        ("[2000-01-01T12:34:56.123456789+00:00]",
         [Datetime(2000, 1, 1, 12, 34, 56, 123456, timezone.utc, nanosecond=789)]),
        ("[2000-01-01T23:59:60Z]",
         [Datetime(2000, 1, 1, 23, 59, 59, 0, timezone.utc, leap_second=True)]),
        ("[2000-01-01T12:34:56.1234567891234+00:00]",
         [Datetime(2000, 1, 1, 12, 34, 56, 123456, timezone.utc, nanosecond=789)]),
        # not datetimes: strings
        ("[2000-1-1, 2023-02-29.log, 2000-01-01T12:34:56+99:99, 2000-01-01T12:34, "
         "2000-01-01T24:00:00Z]",
         ["2000-1-1", "2023-02-29.log", "2000-01-01T12:34:56+99:99",
          "2000-01-01T12:34", "2000-01-01T24:00:00Z"]),
        # binary
        ("[0x[be ef], 0x[beef], 0x[BE, EF], 0x[b], 0b[1010], 0x[], 0o[17], 0o[377]]",
         [b"\xbe\xef", b"\xbe\xef", b"\xbe\xef", b"\x0b", b"\x0a", b"", b"\x0f", b"\xff"]),
        ("[0x[be # comment\nef]]", [b"\xbe\xef"]),
        # ranges
        ("[1..5]", [Range(1, 1, 5)]),
        ("[1..<5]", [Range(1, 1, 5, end_inclusive=False)]),
        ("[1..]", [Range(1, 1, None)]),
        ("[..5]", [Range(0, 1, 5)]),
        ("[..=5]", [Range(0, 1, 5)]),
        ("[1..3..9]", [Range(1, 2, 9)]),
        ("[-5..5]", [Range(-5, 1, 5)]),
        ("[5..1]", [Range(5, -1, 1)]),
        ("[1.5..2.5]", [Range(1.5, 1.0, 2.5)]),
        # cell paths
        ("[$.a.b]", [CellPath([PathMember("a"), PathMember("b")])]),
        ("[$.0]", [CellPath([PathMember(0)])]),
        ("[$.a.0.b]", [CellPath([PathMember("a"), PathMember(0), PathMember("b")])]),
        ("[$.'a b']", [CellPath([PathMember("a b")])]),
        ("[$.a?]", [CellPath([PathMember("a", optional=True)])]),
        ("[$.a!?]", [CellPath([PathMember("a", optional=True, insensitive=True)])]),
        ("[$.a?!]", [CellPath([PathMember("a", optional=True, insensitive=True)])]),
        ("[$.]", [CellPath()]),
        # containers
        ("[1, 2, 3]", [1, 2, 3]),
        ("[1 2 3]", [1, 2, 3]),
        ("[1, 2, 3,]", [1, 2, 3]),
        ("[1,\n2,]", [1, 2]),
        ("[[1, 2], [3]]", [[1, 2], [3]]),
        ("[[1 2 3], 4]", [[1, 2, 3], 4]),
        ("{a: 1 b: 2}", {"a": 1, "b": 2}),
        ("{a:1,b:[1,2]}", {"a": 1, "b": [1, 2]}),
        ("{a: {}}", {"a": {}}),
        ('{k: "a:b"}', {"k": "a:b"}),
        ("{true: 1, null: 2, 1: 3, 2000-01-01: 4}",
         {"true": 1, "null": 2, "1": 3, "2000-01-01": 4}),
        # table form
        ("[[a, b]; [1, 2], [3, 4]]", [{"a": 1, "b": 2}, {"a": 3, "b": 4}]),
        ("[[a b]; [1 2] [3 4]]", [{"a": 1, "b": 2}, {"a": 3, "b": 4}]),
        ("[[1 2]; [3 4]]", [{"1": 3, "2": 4}]),  # T12: headers coerce to strings
        ("[[a]; [[[b]; [1]]]]", [{"a": [{"b": 1}]}]),  # a table cell needs brackets
        ("[[a]; [{x: 1}]]", [{"a": {"x": 1}}]),
        # document level
        ("", None),  # bug 2 is open upstream: the empty document is null
        ("  \n", None),
        ("# only a comment", None),
        ("[1] # trailing comment", [1]),
        ("# leading\n[1]", [1]),
        ("[1] o> /tmp/ignored", [1]),  # a trailing redirection is dropped
        ("| [1]", [1]),
        ("[1 \n| 2]", [1, 2]),  # pipe continues the line, separates items in lists
        ("-abc", "-abc"),  # a `-` prefix is value-like; the word is a string
        # escapes
        ('["a\\u{41}b"]', ["aAb"]),
        ('["\\u{041}"]', ["A"]),
        ('["\\xC3\\xA9", "\\x41"]', ["é", "A"]),
        ('["a\\(b", "a\\#b", "a\\~b", "a\\ab", "a\\ b"]', ["a(b", "a#b", "a~b", "a\ab", "a b"]),
        ('["\\n\\t\\r\\b\\f\\e\\0"]', ["\n\t\r\b\f\x1b\x00"]),
        # five-dots and spread words are strings
        ("[1 ....5, ...]", [1, "....5", "..."]),
        ("[...abc, ...x]", ["...abc", "...x"]),
    ],
)
def test_loads(text, expected):
    assert loads(text) == expected


@pytest.mark.parametrize("text", ["nan", "NaN", "+nan", "-nan"])
def test_loads_nan(text):
    assert math.isnan(loads(text))


def test_loads_inf():
    assert loads("inf") == float("inf")
    assert loads("-inf") == float("-inf")
    assert loads("[INF, inf]") == ["INF", float("inf")]


class TestLoadErrors:
    @pytest.mark.parametrize(
        "text",
        [
            # committed literal errors
            "[0o8]", "[0x]", "[0xg]", "[0xdump]", "[0xdump..5]",
            "[9223372036854775808]",
            "[0x10000000000000000]",
            "[2023-02-29]", "[2000-13-45]", "[2000-04-31]", "[2100-02-29]",
            "[0xns]", "[1zb]", "[1mins]", "[1ab]", "[9999999999999wk]",
            "[8EiB]", "[9223372036854775808ns]", "[1e30ns]", "[1e30sec]",
            "[0o[777]]", "[0b[2]]",
            "[1..1..5]", "[1....5]",
            "[99999999999999999999..5]",
            # top level: calls and structure
            "abc", "abc def", "abc\ndef", "abc; def", "hi", "`hi`",
            "0xdump", "0o8", "1zb", "1z", "2000-1-1", "+abc", "info",
            "`hi`", "not", "if", "match",
            "1 + 2", "[1] [2]", "[1] | [2]", "[1] |",
            "[a && b]", "[1 2]]", "1 = 2",
            # containers
            "{a: 1, a: 2}",
            "{k: a:b}", "{k: http://x.com}",
            "[[a b a]; [1 2 3]]",
            "[[a b];]", "[[a b]; [1]]", "[[a b]; [1 2 3]]",
            "[[a b]; [[c]; [1]]]",
            "[[a]; [...[1]]]",
            # strings
            "['a'\"b\"]", '["a"b]',
            '["\\u0041"]', '["\\u{110000}"]', '["\\u{D800}"]', '["\\u{0000041}"]',
            '["\\xff"]', '["\\xC3"]', '["\\x4"]', '["\\q"]', '["\\"]',
            '[a(b)]', "[$x]", "[$x.y]", "[-(1)]", "[-$x]",
            # unclosed
            "[1, 2", "{a: 1", '["abc', "[0x[be", "r#'abc",
            # spread (parsed, then rejected)
            "[...[1]]", "{...{a: 1}}",
        ],
    )
    def test_raises(self, text):
        with pytest.raises(NuonDecodeError):
            loads(text)

    def test_valid_t12_header_coercion_is_not_an_error(self):
        assert loads("[[a 1]; [1 2]]") == [{"a": 1, "1": 2}]

    def test_error_carries_position(self):
        with pytest.raises(NuonDecodeError) as info:
            loads("[1, 0o8]")
        assert info.value.lineno == 1
        assert info.value.colno == 5
        assert info.value.pos == 4

    def test_year_zero_is_loud(self):
        # nushell (chrono) accepts year 0; Python datetime starts at 1. This
        # is a documented representability limit, not a silent change.
        with pytest.raises(NuonDecodeError, match="year 0"):
            loads("0000-01-01")

    def test_nesting_cap(self):
        with pytest.raises(NuonDecodeError, match="nest"):
            loads("[" * 300 + "]" * 300)

    def test_bytes_input_strict_utf8(self):
        with pytest.raises(NuonDecodeError, match="UTF-8"):
            loads(b"[1, \xff]")

    def test_bom(self):
        assert loads("﻿[1, 2]") == [1, 2]  # one leading BOM is stripped
        with pytest.raises(NuonDecodeError):
            loads("﻿﻿[1]")  # only one


class TestDeferredConversionErrors:
    """The `not supported in nuon` family only surfaces in an otherwise
    clean document: a parse error anywhere else wins (from.rs order)."""

    def test_interpolation(self):
        with pytest.raises(NuonDecodeError, match="interpolation"):
            loads("[a(b)]")

    def test_parse_error_beats_conversion_error(self):
        with pytest.raises(NuonDecodeError, match="radix"):
            loads("[a(b), 0o8]")

    def test_raw_string_key(self):
        with pytest.raises(NuonDecodeError, match="only strings can be keys"):
            loads("{r#'k'#: 1}")

    def test_raw_string_header(self):
        with pytest.raises(NuonDecodeError, match="only strings can be keys"):
            loads("[[r#'k'#]; [1]]")


class TestRecordOrder:
    def test_key_order_is_preserved(self):
        assert list(loads("{b: 1, a: 2, c: 3}")) == ["b", "a", "c"]

    def test_table_column_order_is_preserved(self):
        rows = loads("[[x, y, z]; [1, 2, 3]]")
        assert list(rows[0]) == ["x", "y", "z"]
