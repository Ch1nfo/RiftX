"""Deterministic process fixture used by runner integration tests."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=[
            "success",
            "failure",
            "sleep",
            "stream",
            "large",
            "child",
            "stubborn-child",
            "setsid-double-fork",
        ],
    )
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--heartbeat")
    parser.add_argument("--pid-file")
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

    if args.mode == "setsid-double-fork":
        if os.name != "posix":
            parser.error("setsid-double-fork requires POSIX")
        if not args.heartbeat or not args.pid_file:
            parser.error("--heartbeat and --pid-file are required")
        first_child = os.fork()
        if first_child == 0:
            os.setsid()
            second_child = os.fork()
            if second_child != 0:
                os._exit(0)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            Path(args.pid_file).write_text(str(os.getpid()), encoding="utf-8")
            heartbeat = Path(args.heartbeat)
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                with heartbeat.open("a") as stream:
                    stream.write("x")
                time.sleep(0.05)
            os._exit(0)
        os.waitpid(first_child, 0)
        time.sleep(args.seconds)
        return 0

    if not args.heartbeat:
        parser.error("--heartbeat is required in child mode")
    heartbeat = Path(args.heartbeat)
    signal_setup = (
        "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        if args.mode == "stubborn-child"
        else ""
    )
    child_code = (
        signal_setup + "import pathlib,time; p=pathlib.Path(" + repr(str(heartbeat)) + "); "
        "[(p.open('a').write('x'), time.sleep(0.05)) for _ in range(1200)]"
    )
    child = subprocess.Popen([sys.executable, "-c", child_code])
    print(child.pid, flush=True)
    time.sleep(args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
