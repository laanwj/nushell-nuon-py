"""Multiply every pipeline value by ``params.factor``.

The runnable companion to ``py-call.nu``; the minimal interface it
implements is in that file's help text.
"""

import sys

try:
    import nushell_nuon
except ImportError:  # running the example from the repo without installing
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import nushell_nuon


def main() -> int:
    request = nushell_nuon.loads(sys.stdin.read())
    factor = request["params"]["factor"]
    print(nushell_nuon.dumps([value * factor for value in request["input"]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
