"""Multiply every pipeline value by ``params.factor``.

The runnable companion to ``py-call.nu``; the minimal interface it
implements is in that file's help text.
"""

import sys

try:
    import nuon
except ImportError:  # running the example from the repo without installing
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import nuon


def main() -> int:
    request = nuon.loads(sys.stdin.read())
    factor = request["params"]["factor"]
    print(nuon.dumps([value * factor for value in request["input"]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
