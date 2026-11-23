from datetime import datetime, timedelta, timezone

import pytest

from nuon import (
    CellPath,
    Datetime,
    Duration,
    Filesize,
    NuonEncodeError,
    PathMember,
    Range,
    dumps,
)


class TestScalars:
    def test_none_bool(self):
        assert dumps(None) == "null"
        assert dumps(True) == "true"
        assert dumps(False) == "false"

    def test_int(self):
        assert dumps(5) == "5"
        assert dumps(-9223372036854775808) == "-9223372036854775808"
        assert dumps(9223372036854775807) == "9223372036854775807"

    def test_int_out_of_range_is_loud(self):
        with pytest.raises(NuonEncodeError, match="64 bits"):
            dumps(9223372036854775808)
        with pytest.raises(NuonEncodeError, match="64 bits"):
            dumps(-9223372036854775809)

    def test_units(self):
        assert dumps(Duration(1_500_000_000)) == "1500000000ns"
        assert dumps(Duration(-1000)) == "-1000ns"
        assert dumps(Filesize(1024)) == "1024b"
        assert dumps(Filesize(-1000)) == "-1000b"
        assert dumps(timedelta(seconds=1)) == "1000000000ns"
        assert dumps(timedelta(microseconds=-2)) == "-2000ns"

    def test_floats_never_use_an_exponent(self):
        assert dumps(1.0) == "1.0"
        assert dumps(-0.0) == "-0.0"
        assert dumps(0.1) == "0.1"
        assert dumps(1e-5) == "0.00001"
        assert dumps(1e-30) == "0.000000000000000000000000000001"
        assert dumps(1e15) == "1000000000000000.0"
        assert dumps(1e16) == "10000000000000000.0"
        assert dumps(1e-300) == "0." + "0" * 299 + "1"
        assert dumps(float("inf")) == "inf"
        assert dumps(float("-inf")) == "-inf"
        assert dumps(float("nan")) == "NaN"

    def test_binary(self):
        assert dumps(b"\xde\xad\xbe\xef") == "0x[DEADBEEF]"
        assert dumps(b"") == "0x[]"
        assert dumps(bytearray(b"\x0b")) == "0x[0B]"

    def test_datetime_autosi(self):
        utc = timezone.utc
        assert dumps(Datetime(2000, 1, 1, tzinfo=utc)) == "2000-01-01T00:00:00+00:00"
        assert dumps(
            Datetime(2000, 1, 1, 12, 34, 56, 123000, utc)
        ) == "2000-01-01T12:34:56.123+00:00"
        assert dumps(
            Datetime(2000, 1, 1, 12, 34, 56, 123456, utc)
        ) == "2000-01-01T12:34:56.123456+00:00"
        assert dumps(
            Datetime(2000, 1, 1, 12, 34, 56, 123456, utc, nanosecond=789)
        ) == "2000-01-01T12:34:56.123456789+00:00"
        # trailing zeros stay in the shortest form that keeps the value
        assert dumps(
            Datetime(2000, 1, 1, 12, 34, 56, 100000, utc)
        ) == "2000-01-01T12:34:56.100+00:00"
        assert dumps(
            Datetime(2000, 1, 1, 12, 34, 56, 120000, utc)
        ) == "2000-01-01T12:34:56.120+00:00"

    def test_datetime_leap_second_and_offset(self):
        utc = timezone.utc
        assert dumps(
            Datetime(2000, 1, 1, 23, 59, 59, 0, utc, leap_second=True)
        ) == "2000-01-01T23:59:60+00:00"
        assert dumps(
            Datetime(2000, 1, 1, 23, 59, 59, 500000, utc, leap_second=True)
        ) == "2000-01-01T23:59:60.500+00:00"
        tz = timezone(timedelta(hours=5, minutes=30))
        assert dumps(Datetime(2000, 1, 1, 12, 34, 56, 123000, tz)) == (
            "2000-01-01T12:34:56.123+05:30"
        )

    def test_naive_datetime_is_utc(self):
        assert dumps(datetime(2000, 1, 1)) == "2000-01-01T00:00:00+00:00"

    def test_non_minute_offset_is_loud(self):
        from datetime import datetime as dt

        bad = dt(2000, 1, 1, tzinfo=timezone(timedelta(hours=5, seconds=30)))
        with pytest.raises(NuonEncodeError, match="offset"):
            dumps(bad)

    def test_cell_paths(self):
        assert dumps(CellPath([PathMember("a"), PathMember("b")])) == "$.a.b"
        assert dumps(CellPath([PathMember(0)])) == "$.0"
        assert dumps(CellPath([PathMember("a b")])) == '$."a b"'
        assert dumps(CellPath([PathMember("a", optional=True)])) == "$.a?"
        assert dumps(CellPath([PathMember("a", insensitive=True)])) == "$.a!"
        assert dumps(
            CellPath([PathMember("a", optional=True, insensitive=True)])
        ) == "$.a!?"
        assert dumps(CellPath([PathMember(0, optional=True)])) == "$.0?"
        assert dumps(CellPath()) == "$."

    def test_ranges(self):
        assert dumps(Range(1, 1, 5)) == "1..5"
        assert dumps(Range(1, 1, 5, end_inclusive=False)) == "1..<5"
        assert dumps(Range(1, 1, None)) == "1.."
        assert dumps(Range(0, 1, 5)) == "0..5"
        assert dumps(Range(1, 2, 9)) == "1..3..9"
        assert dumps(Range(5, -1, 1)) == "5..4..1"
        assert dumps(Range(1.5, 1.0, 2.5)) == "1.5..2.5"
        assert dumps(Range(1.0, 1.0, 2.0)) == "1.0..2.0"
        assert dumps(Range(1.0, 0.5, 2.0)) == "1.0..1.5..2.0"


class TestStrings:
    def test_top_level_always_quoted(self):
        assert dumps("hi") == '"hi"'
        assert dumps("abc") == '"abc"'

    def test_bare_when_nested(self):
        assert dumps(["abc"]) == "[abc]"
        assert dumps({"k": "abc"}) == "{k: abc}"

    def test_digit_rule_forces_quoting(self):
        assert dumps(["a1", "0x", "x-1", "a-b", "a_b", "E5", "1s", "3/4"]) == (
            '["a1", "0x", "x-1", a-b, a_b, "E5", "1s", "3/4"]'
        )

    def test_unicode_digit_and_whitespace(self):
        assert dumps(["a０", "a٣", "a½", "Ⅴ", "௰"]) == '["a０", "a٣", a½, Ⅴ, ௰]'
        assert dumps(["a b", "a　b", "a​b"]) == '["a b", "a　b", a​b]'

    def test_keywords_and_float_names(self):
        assert dumps(["true", "false", "null", "&&"]) == (
            '["true", "false", "null", "&&"]'
        )
        assert dumps(["INFINITY", "Inf", "NAN", "info", "nan", "inf"]) == (
            '["INFINITY", "Inf", "NAN", info, "nan", "inf"]'
        )

    def test_nul_is_escaped_and_quoted(self):
        assert dumps(["a\x00b"]) == '["a\\0b"]'
        assert dumps("a\x00b") == '"a\\0b"'

    def test_newline_written_verbatim(self):
        assert dumps(["a\nb"]) == '["a\nb"]'

    def test_raw_strings(self):
        assert dumps('hello "world"', raw_strings=True) == "r#'hello \"world\"'#"
        assert dumps("hello world", raw_strings=True) == '"hello world"'
        assert dumps("has 'single'", raw_strings=True) == '"has \'single\'"'
        assert dumps('#"foo', raw_strings=True) == 'r##\'#"foo\'##'
        assert dumps('##"foo', raw_strings=True) == 'r###\'##"foo\'###'
        # keys never use the raw form
        assert dumps({'k"': 1}, raw_strings=True) == '{"k\\"": 1}'

    def test_raw_string_never_for_nul(self):
        assert dumps('a\x00"b', raw_strings=True) == '"a\\0\\"b"'


class TestStyles:
    def test_default(self):
        assert dumps([1, 2, 3]) == "[1, 2, 3]"
        assert dumps({"a": 1, "b": [1, 2]}) == "{a: 1, b: [1, 2]}"

    def test_raw(self):
        assert dumps([1, 2, 3], raw=True) == "[1,2,3]"
        assert dumps({"a": 1}, raw=True) == "{a:1}"

    def test_indent(self):
        assert dumps([1, 2], indent=2) == "[\n  1,\n  2\n]"
        assert dumps({"a": 1, "b": 2}, indent=2) == "{\n  a: 1,\n  b: 2\n}"
        assert dumps([1, 2], indent=4) == "[\n    1,\n    2\n]"

    def test_indent_zero_means_raw(self):
        assert dumps([[1, 2], [3, 4]], indent=0) == "[[1,2],[3,4]]"

    def test_tabs(self):
        assert dumps([1, 2], tabs=1) == "[\n\t1,\n\t2\n]"
        assert dumps([[1, 2], [3, 4]], tabs=1) == (
            "[\n\t[\n\t\t1,\n\t\t2\n\t],\n\t[\n\t\t3,\n\t\t4\n\t]\n]"
        )

    def test_pretty_is_indent_2(self):
        assert dumps([1, 2], pretty=True) == dumps([1, 2], indent=2)

    def test_no_commas(self):
        assert dumps([1, 2, 3], commas=False) == "[1 2 3]"
        assert dumps([True, False, None], commas=False) == "[true false null]"
        assert dumps({"a": 1, "b": 2}, commas=False) == "{a: 1 b: 2}"

    def test_raw_wins_over_indent_and_pretty(self):
        assert dumps([[1, 2]], raw=True, indent=4) == "[[1,2]]"
        assert dumps([[1, 2]], raw=True, pretty=True) == "[[1,2]]"

    def test_tabs_win_over_indent(self):
        assert dumps([1], indent=4, tabs=1) == "[\n\t1\n]"

    def test_raw_rejects_no_commas(self):
        with pytest.raises(ValueError):
            dumps([1, 2], raw=True, commas=False)

    def test_empty_container_under_indent_has_the_blank_line(self):
        # upstream bug 4, reproduced on purpose
        assert dumps({"a": {}}, indent=2) == "{\n  a: {\n\n  }\n}"
        assert dumps([[], {}], indent=2) == "[\n  [\n\n  ],\n  {\n\n  }\n]"

    def test_list_of_records_suppresses_the_table(self):
        rows = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        assert dumps(rows, list_of_records=True) == "[{a: 1, b: 2}, {a: 3, b: 4}]"
        assert dumps(rows, list_of_records=True, indent=2) == (
            "[\n  {a: 1, b: 2},\n  {a: 3, b: 4}\n]"
        )
        assert dumps(rows, list_of_records=True, raw=True) == "[{a:1,b:2},{a:3,b:4}]"


class TestTables:
    ROWS = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]

    def test_compact(self):
        assert dumps(self.ROWS) == "[[a, b]; [1, 2], [3, 4]]"

    def test_pretty_aligns(self):
        assert dumps(self.ROWS, pretty=True) == (
            "[\n  [a, b];\n  [1, 2],\n  [3, 4]\n]"
        )

    def test_aligned_columns(self):
        rows = [
            {"name": "Alice", "age": 30},
            {"name": "Bob", "age": 25},
        ]
        assert dumps(rows, pretty=True) == (
            "[\n  [name,  age];\n  [Alice, 30],\n  [Bob,   25]\n]"
        )

    def test_cjk_alignment(self):
        rows = [{"a": "日本", "b": 1}, {"a": "abcde", "b": 2}]
        assert dumps(rows, pretty=True) == (
            '[\n  [a,     b];\n  [日本,  1],\n  [abcde, 2]\n]'
        )

    def test_emoji_alignment(self):
        rows = [{"a": "🎉", "b": 1}, {"a": "abcde", "b": 2}]
        assert dumps(rows, pretty=True) == (
            '[\n  [a,     b];\n  [🎉,    1],\n  [abcde, 2]\n]'
        )

    def test_zwj_family_is_two_cells(self):
        rows = [{"a": "👨‍👩‍👦", "b": 1}, {"a": "abcde", "b": 2}]
        assert dumps(rows, pretty=True) == (
            '[\n  [a,     b];\n  [👨‍👩‍👦,    1],\n  [abcde, 2]\n]'
        )

    def test_flag_is_two_cells(self):
        rows = [{"a": "🇩🇪", "b": 1}, {"a": "abcde", "b": 2}]
        assert dumps(rows, pretty=True) == (
            '[\n  [a,     b];\n  [🇩🇪,    1],\n  [abcde, 2]\n]'
        )

    def test_vs16_is_two_cells(self):
        rows = [{"a": "✌️", "b": 1}, {"a": "abcde", "b": 2}]
        assert dumps(rows, pretty=True) == (
            '[\n  [a,     b];\n  [✌️,    1],\n  [abcde, 2]\n]'
        )

    def test_multiline_cell_counts_its_full_length(self):
        rows = [{"a": {"x": 1}}]
        assert dumps(rows, pretty=True) == (
            "[\n  [a];\n  [{\n      x: 1\n    }]\n]"
        )

    def test_nested_table_cell_needs_brackets(self):
        rows = [{"a": [{"b": 1}]}]
        assert dumps(rows) == "[[a]; [[[b]; [1]]]]"

    def test_not_a_table_when_order_differs(self):
        rows = [{"a": 1, "b": 2}, {"b": 3, "a": 4}]
        assert dumps(rows) == "[{a: 1, b: 2}, {b: 3, a: 4}]"

    def test_not_a_table_for_empty_records(self):
        assert dumps([{}, {}]) == "[{}, {}]"
        assert dumps([]) == "[]"

    def test_raw_table(self):
        assert dumps(self.ROWS, raw=True) == "[[a,b];[1,2],[3,4]]"

    def test_indent_0_table_is_raw(self):
        assert dumps(self.ROWS, indent=0) == "[[a,b];[1,2],[3,4]]"

    def test_indent_4(self):
        assert dumps(self.ROWS, indent=4) == (
            "[\n    [a, b];\n    [1, 2],\n    [3, 4]\n]"
        )


class TestEncodeErrors:
    def test_unknown_type(self):
        with pytest.raises(TypeError):
            dumps(object())

    def test_record_key_must_be_str(self):
        with pytest.raises(NuonEncodeError, match="keys must be strings"):
            dumps({1: "x"})

    def test_nesting_cap(self):
        with pytest.raises(NuonEncodeError, match="nest"):
            deep = []
            cur = deep
            for _ in range(300):
                cur.append([])
                cur = cur[-1]
            dumps(deep)

    def test_error_path_points_at_the_value(self):
        with pytest.raises(NuonEncodeError) as info:
            dumps({"a": [1, {"b": 2**63}]})
        assert info.value.path == "$.a[1].b"


class TestRoundTrips:
    @pytest.mark.parametrize(
        "value",
        [
            None,
            True,
            5,
            -9223372036854775808,
            0.1,
            -0.0,
            float("inf"),
            "hi",
            "a b",
            "a\x00b",
            b"\xde\xad",
            [1, "two", None, True],
            {"k": [1, {"n": 2.5}]},
            [{"a": 1}, {"a": 2}],
            Duration(1234567),
            Filesize(-4096),
            Datetime(2000, 1, 1, 12, 34, 56, 123456, timezone.utc, nanosecond=789),
            CellPath([PathMember("a", optional=True), PathMember(3)]),
            Range(1, 2, 9),
            Range(1.5, 1.0, 2.5, end_inclusive=False),
        ],
    )
    def test_round_trip(self, value):
        from nuon import loads

        text = dumps(value)
        assert loads(text) == value, text
        assert dumps(loads(text)) == text
