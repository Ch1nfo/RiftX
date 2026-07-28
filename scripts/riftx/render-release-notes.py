#!/usr/bin/env python3
"""Extract a non-placeholder release section from CHANGELOG.md."""

import argparse
from pathlib import Path
import re
import sys

PLACEHOLDERS = ("TBD", "will be filled", "待补充")


def extract_release_notes(changelog: str, version: str) -> str:
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\](?:\s+-\s+[^\n]+)?\s*$\n(.*?)(?=^## \[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(changelog)
    if match is None:
        raise ValueError(f"CHANGELOG.md has no [{version}] section")
    body = match.group(1).strip()
    if not body:
        raise ValueError(f"CHANGELOG.md [{version}] section is empty")
    if any(placeholder.lower() in body.lower() for placeholder in PLACEHOLDERS):
        raise ValueError(f"CHANGELOG.md [{version}] section is still a placeholder")
    return f"# RiftX {version}\n\n{body}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        notes = extract_release_notes(
            args.changelog.read_text(encoding="utf-8"), args.version
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(notes, encoding="utf-8")
    except (OSError, ValueError) as error:
        print(f"release notes generation failed: {error}", file=sys.stderr)
        return 1
    print(f"release notes written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
