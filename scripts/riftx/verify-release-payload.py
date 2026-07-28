#!/usr/bin/env python3
"""Scan release staging trees for secrets, debug artifacts, and temporary endpoints."""

import argparse
from pathlib import Path
import re
import sys

FORBIDDEN_SUFFIXES = {
    ".core",
    ".dmp",
    ".har",
    ".ilk",
    ".pdb",
    ".profraw",
}
FORBIDDEN_PATH_PARTS = {
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".riftx",
    "node_modules",
    "target",
}
FORBIDDEN_FILENAMES = {
    "request-dump.json",
    "response-dump.json",
    "debug.log",
}
FORBIDDEN_MARKERS = {
    b"native-acceptance-secret": "native acceptance API key",
    b"native-acceptance-secondary-secret": "secondary acceptance API key",
    b"runtime-smoke-not-a-real-key": "runtime smoke API key",
    b"not-a-real-key": "test API key marker",
    b"smoke-key": "smoke API key marker",
    b"example.test": "temporary test endpoint",
}
SECRET_PATTERNS = (
    (re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"), "OpenAI-style secret"),
    (re.compile(rb"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (
        re.compile(rb"https?://[^\x00-\x20\"']+\.(?:internal|corp)(?:[/:]|$)", re.I),
        "internal URL",
    ),
)
MAX_SCAN_BYTES = 512 * 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    return parser.parse_args()


def scan_paths(paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        if not path.exists():
            errors.append(f"missing release payload path: {path}")
            continue
        if path.is_symlink():
            errors.append(f"symbolic link is not allowed in a release payload: {path}")
            continue
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                errors.extend(scan_entry(child, path))
        else:
            errors.extend(scan_entry(path, path.parent))
    return errors


def scan_entry(path: Path, root: Path) -> list[str]:
    relative = path.relative_to(root)
    errors: list[str] = []
    if path.is_symlink():
        return [f"symbolic link is not allowed in a release payload: {relative}"]
    if any(part in FORBIDDEN_PATH_PARTS for part in relative.parts):
        errors.append(f"development-only path is present: {relative}")
    if path.name in FORBIDDEN_FILENAMES:
        errors.append(f"debug/request dump file is present: {relative}")
    if path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name.endswith(".dSYM"):
        errors.append(f"debug artifact is present: {relative}")
    if path.is_dir() or errors:
        return errors

    size = path.stat().st_size
    if size > MAX_SCAN_BYTES:
        return [
            f"release file exceeds scanner limit ({MAX_SCAN_BYTES} bytes): {relative}"
        ]
    data = path.read_bytes()
    lowered = data.lower()
    for marker, label in FORBIDDEN_MARKERS.items():
        if marker.lower() in lowered:
            errors.append(f"{label} found in {relative}")
    for pattern, label in SECRET_PATTERNS:
        if pattern.search(data):
            errors.append(f"{label} found in {relative}")
    return errors


def main() -> int:
    args = parse_args()
    errors = scan_paths(args.paths)
    if errors:
        for error in errors:
            print(f"release payload validation failed: {error}", file=sys.stderr)
        return 1
    print("release payload scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
