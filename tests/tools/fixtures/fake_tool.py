"""Tool Registry version-probe fixture."""

from __future__ import annotations

import sys
import time


def main() -> int:
    if "--slow-version" in sys.argv:
        time.sleep(30)
        print("fake-tool 1.2.3")
        return 0
    if "--bad-version" in sys.argv:
        print("probe failed", file=sys.stderr)
        return 7
    if "--large" in sys.argv:
        sys.stdout.write("z" * 100_000)
        sys.stdout.flush()
        return 0
    if "--version" in sys.argv:
        print("fake-tool 1.2.3")
        return 0
    print("args=" + "|".join(sys.argv[1:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
