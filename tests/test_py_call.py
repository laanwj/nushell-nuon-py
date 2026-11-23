"""End-to-end test of the py-call.nu wrapper against a real nushell binary.

Covers the transport contract: typed values crossing both ways, the
explicit-null and empty-output distinction, and loud failures.
"""

import os
import subprocess
import tempfile

import pytest

import nushell_nuon

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")
WRAPPER = os.path.join(EXAMPLES, "py-call.nu")
DOUBLE = os.path.join(EXAMPLES, "double.py")
REPO = os.path.join(EXAMPLES, "..")


def _find_nu() -> str | None:
    for candidate in [
        os.environ.get("NU_BIN"),
        "/tmp/nu-target/debug/nu",
        subprocess.run(
            ["which", "nu"], capture_output=True, text=True
        ).stdout.strip(),
    ]:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


pytestmark = pytest.mark.skipif(_find_nu() is None, reason="no nushell binary")


def py_call(pipeline: str) -> tuple[int, str, str]:
    """Run `pipeline` with the wrapper sourced; returns rc, stdout, stderr.

    Pipelines should end in `| to nuon --raw` so the returned value is
    captured as text rather than rendered as a display table."""
    script = f"source {WRAPPER!r}; {pipeline}"
    result = subprocess.run(
        [_find_nu(), "-n", "-c", script],  # type: ignore[list-item]
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, HOME=tempfile.mkdtemp(prefix="py-call-")),
    )
    return result.returncode, result.stdout.rstrip("\n"), result.stderr


def echo_params_script() -> str:
    path = os.path.join(tempfile.mkdtemp(), "echo-params.py")
    with open(path, "w") as f:
        f.write(
            "import sys\n"
            f"sys.path.insert(0, {REPO!r})\n"
            "import nushell_nuon\n"
            "request = nushell_nuon.loads(sys.stdin.read())\n"
            "print(nushell_nuon.dumps(request['params']))\n"
        )
    return path


def test_numbers():
    rc, out, _ = py_call(f"[1 2 3] | py-call {DOUBLE!r} {{factor: 2}} | to nuon --raw")
    assert rc == 0
    assert nushell_nuon.loads(out) == [2, 4, 6]


def test_floats():
    rc, out, _ = py_call(f"[0.5 1.5] | py-call {DOUBLE!r} {{factor: 3}} | to nuon --raw")
    assert rc == 0
    assert nushell_nuon.loads(out) == [1.5, 4.5]


def test_durations_cross_natively():
    rc, out, _ = py_call(f"[2hr 3min] | py-call {DOUBLE!r} {{factor: 2}} | to nuon --raw")
    assert rc == 0
    assert nushell_nuon.loads(out) == [
        nushell_nuon.Duration(4 * 3600 * 10**9), nushell_nuon.Duration(6 * 60 * 10**9)
    ]


def test_typed_params_cross_natively():
    # a datetime, duration, filesize and range as parameters: they must
    # arrive in Python as their nushell_nuon types and survive the trip back
    params = (
        "{factor: 2, when: 2020-05-05T12:00:00+02:00, "
        "span: 5sec, size: 2kib, window: 1..5}"
    )
    rc, out, _ = py_call(f"null | py-call {echo_params_script()!r} {params} | to nuon --raw")
    assert rc == 0
    from datetime import timedelta, timezone

    import nushell_nuon as n

    assert n.loads(out) == {
        "factor": 2,
        "when": n.Datetime(2020, 5, 5, 12, 0, 0, 0, timezone(timedelta(hours=2))),
        "span": n.Duration(5 * 10**9),
        "size": n.Filesize(2048),
        "window": n.Range(1, 1, 5),
    }


def test_multiline_string_in_params():
    # a value with a newline makes the stdin document multi-line; the whole
    # stdin is one document, so it must still parse
    rc, out, _ = py_call(
        f'["a\\nb"] | py-call {DOUBLE!r} {{factor: 1}} | to nuon --raw'
    )
    assert rc == 0
    assert nushell_nuon.loads(out) == ["a\nb"]


def test_binary_input():
    rc, out, _ = py_call(f"[0x[be ef]] | py-call {DOUBLE!r} {{factor: 1}} | to nuon --raw")
    assert rc == 0
    assert nushell_nuon.loads(out) == [b"\xbe\xef"]


def test_missing_params_fails_loudly():
    # double.py requires params.factor; the KeyError must not be swallowed
    rc, _, err = py_call(f"[1] | py-call {DOUBLE!r}")
    assert rc != 0
    assert "KeyError" in err or "params" in err


def test_empty_output_decodes_as_null():
    # the minimal wrapper inherits `from nuon`'s empty-document behavior:
    # a script that prints nothing returns nothing
    script = os.path.join(tempfile.mkdtemp(), "empty.py")
    with open(script, "w") as f:
        f.write("import sys\nsys.stdout.write('')\n")
    rc, out, _ = py_call(f"1 | py-call {script!r} | describe")
    assert rc == 0
    assert out == "nothing"


def test_explicit_null_return():
    script = os.path.join(tempfile.mkdtemp(), "none.py")
    with open(script, "w") as f:
        f.write(
            "import sys\n"
            f"sys.path.insert(0, {REPO!r})\n"
            "import nushell_nuon\n"
            "print(nushell_nuon.dumps(None))\n"
        )
    rc, out, _ = py_call(f"1 | py-call {script!r} | describe")
    assert rc == 0
    assert out == "nothing"


def test_python_override():
    rc, out, _ = py_call(
        f"$env.PY_CALL_PYTHON = 'python3'; [2] | py-call {DOUBLE!r} {{factor: 5}} | to nuon --raw"
    )
    assert rc == 0
    assert nushell_nuon.loads(out) == [10]


# examples/tour.py: the full type surface, both directions


TOUR = os.path.join(EXAMPLES, "tour.py")


def _tour(pipeline: str) -> dict:
    rc, out, _ = py_call(f"{pipeline} | py-call {TOUR!r} | to nuon --raw")
    assert rc == 0
    return nushell_nuon.loads(out)


def test_tour_duration_and_sample_types():
    result = _tour("2hr")
    assert result["saw"] == {"kind": "duration", "ns": 7_200_000_000_000}
    sample = result["sample"]
    assert sample["for"] == nushell_nuon.Duration(1_500_000_000)
    assert sample["disk"] == nushell_nuon.Filesize(2 * 1024**3)
    assert sample["every"] == nushell_nuon.Range(1, 2, 9)
    assert sample["blob"] == b"\xde\xad\xbe\xef"


def test_tour_datetime_nanosecond_round_trip():
    result = _tour("2000-01-01")
    from datetime import timezone as tz

    assert result["saw"]["kind"] == "datetime"
    assert result["sample"]["at"] == nushell_nuon.Datetime(
        2026, 1, 1, 12, 0, 0, 0, tz.utc, nanosecond=500
    )


def test_tour_record_columns_and_cell_path():
    result = _tour("{name: Alice, tags: [a b]}")
    assert result["saw"] == {"kind": "record", "columns": ["name", "tags"]}
    assert result["sample"]["where"] == nushell_nuon.CellPath(
        [nushell_nuon.PathMember("user"),
         nushell_nuon.PathMember("name", optional=True)]
    )


def test_tour_nested_list_of_records():
    result = _tour("[{a: 1}, {b: 2sec}]")
    assert result["saw"] == {
        "kind": "list", "length": 2, "elements": ["record", "record"]
    }


def test_tour_bool_before_int():
    result = _tour("[true, 1]")
    assert result["saw"]["elements"] == ["bool", "int"]
