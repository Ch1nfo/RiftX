"""Deterministic process fixture used by runner integration tests."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["success", "failure", "sleep", "stream", "large", "child"])
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--heartbeat")
    args = parser.parse_args()

    if args.mode == "success":
        print("stdout: 你好 RiftX", flush=True)
        print(f"env: {os.getenv('RIFTX_TEST_VALUE', '')}", flush=True)
        print("stderr: diagnostic", file=sys.stderr, flush=True)
        return 0
    if args.mode == "failure":
        print("intentional failure", file=sys.stderr, flush=True)
        return 23
    if args.mode == "sleep":
        time.sleep(args.seconds)
        return 0
    if args.mode == "stream":
        print("first", flush=True)
        time.sleep(0.25)
        print("second", flush=True)
        return 0
    if args.mode == "large":
        sys.stdout.write("x" * 200_000)
        sys.stdout.flush()
        return 0

    if not args.heartbeat:
        parser.error("--heartbeat is required in child mode")
    heartbeat = Path(args.heartbeat)
    child_code = (
        "import pathlib,time; p=pathlib.Path(" + repr(str(heartbeat)) + "); "
        "[(p.open('a').write('x'), time.sleep(0.05)) for _ in range(1200)]"
    )
    child = subprocess.Popen([sys.executable, "-c", child_code])
    print(child.pid, flush=True)
    time.sleep(args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
