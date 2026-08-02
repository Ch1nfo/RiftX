#!/usr/bin/env python3
"""Check the independently implemented RiftX Code Audit production boundary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from riftx.evaluation import IndependenceBoundaryScanner  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="RiftX repository root (defaults to the repository containing this script)",
    )
    parser.add_argument(
        "--artifact",
        "--bundle",
        dest="artifacts",
        action="append",
        type=Path,
        default=[],
        help="Explicit build artifact, bundle directory, wheel, JAR, ZIP, or tarball to inspect",
    )
    parser.add_argument(
        "--require-artifact",
        action="store_true",
        help="Fail unless at least one explicit --artifact/--bundle target is supplied",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    report = IndependenceBoundaryScanner().scan(
        arguments.root,
        artifacts=arguments.artifacts,
        require_artifact=arguments.require_artifact,
    )
    print(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
