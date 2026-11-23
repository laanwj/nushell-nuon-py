from datetime import datetime, timedelta, timezone

import pytest

from nuon import CellPath, Datetime, Duration, Filesize, PathMember, Range


class TestFilesize:
    def test_is_an_int(self):
        f = Filesize(1024)
        assert f == 1024
        assert isinstance(f, int)
        assert f + 1 == 1025
        assert repr(f) == "Filesize(1024)"

    def test_negative_matches_nushell(self):
        # bug 9 is still open upstream: filesizes may be negative
        assert Filesize(-1000) == -1000


class TestDuration:
    def test_ns_roundtrip_exact(self):
        assert Duration(1_500_000_000).total_ns == 1_500_000_000
        assert Duration(0).total_ns == 0
        assert Duration(1).total_ns == 1

    def test_submicrosecond_remainder(self):
        d = Duration(1_500)
        assert d == timedelta(microseconds=1)
        assert d.nanosecond == 500
        assert d.total_ns == 1_500

    def test_negative_decomposition(self):
        d = Duration(-1_500)
        assert d.nanosecond == 500
        assert d == timedelta(microseconds=-2)
        assert d.total_ns == -1_500

    def test_large_in_range(self):
        ns = 9_223_372_036_854_775_807
        d = Duration(ns)
        assert d.total_ns == ns
        assert d.days == 106751

    def test_is_a_timedelta(self):
        assert isinstance(Duration(5), timedelta)
        assert Duration(1_000_000_000) == timedelta(seconds=1)
        assert repr(Duration(1500)) == "Duration(1500)"

    def test_construction_from_timedelta_not_supported(self):
        # the constructor takes the nanosecond count; a bare timedelta is
        # accepted by dumps() directly instead
        with pytest.raises(TypeError):
            Duration(timedelta(seconds=1))  # type: ignore[arg-type]


class TestDatetime:
    def test_is_a_datetime(self):
        dt = Datetime(2000, 1, 1, 12, 30, 45, tzinfo=timezone.utc)
        assert isinstance(dt, datetime)
        assert dt.year == 2000 and dt.hour == 12
        assert dt.utcoffset() == timedelta(0)

    def test_nanosecond(self):
        dt = Datetime(2000, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc, nanosecond=789)
        assert dt.microsecond == 123456
        assert dt.nanosecond == 789

    def test_leap_second(self):
        dt = Datetime(2000, 1, 1, 23, 59, 59, tzinfo=timezone.utc, leap_second=True)
        assert dt.second == 59
        assert dt.leap_second
        assert ":60" in repr(dt)

    def test_repr_full_precision(self):
        dt = Datetime(2000, 1, 1, 0, 0, 0, 123456, timezone.utc, nanosecond=789)
        assert repr(dt) == "Datetime('2000-01-01T00:00:00.123456789+00:00')"

    def test_offset(self):
        from datetime import timedelta

        tz = timezone(timedelta(hours=5, minutes=30))
        dt = Datetime(2000, 1, 1, tzinfo=tz)
        assert repr(dt) == "Datetime('2000-01-01T00:00:00+05:30')"


class TestPathMemberAndCellPath:
    def test_member_kinds(self):
        assert PathMember("a") == PathMember("a", False, False)
        assert PathMember("a", optional=True) != PathMember("a")
        assert PathMember("a", insensitive=True).insensitive

    def test_member_rejects_bool(self):
        with pytest.raises(TypeError):
            PathMember(True)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            PathMember(1.5)  # type: ignore[arg-type]

    def test_cellpath_coerces_str_and_int(self):
        cp = CellPath(["a", 0, "b"])
        assert cp.members == (PathMember("a"), PathMember(0), PathMember("b"))
        assert len(cp) == 3

    def test_empty_cellpath(self):
        assert CellPath() == CellPath([])
        assert len(CellPath()) == 0

    def test_equality_and_iter(self):
        cp = CellPath([PathMember("a", optional=True)])
        assert cp == CellPath([PathMember("a", optional=True)])
        assert cp != CellPath(["a"])
        assert list(cp) == [PathMember("a", optional=True)]

    def test_no_syntax_interpretation_on_coercion(self):
        # coercing a bare string does not parse cell-path syntax
        cp = CellPath(["a?"])
        assert cp.members == (PathMember("a?"),)


class TestRange:
    def test_defaults(self):
        r = Range()
        assert r.start == 0 and r.step == 1 and r.end is None and r.end_inclusive

    def test_frozen(self):
        r = Range(1, 1, 5)
        with pytest.raises(Exception):
            r.start = 2  # type: ignore[misc]

    def test_step_zero_rejected(self):
        with pytest.raises(ValueError):
            Range(1, 0, 5)

    def test_examples(self):
        assert Range(1, 1, 5) == Range(1, 1, 5, True)
        assert Range(1, 1, 5, end_inclusive=False) != Range(1, 1, 5)
        assert Range(1, 2, 9) == Range(1, 2, 9)
