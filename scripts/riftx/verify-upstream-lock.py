#!/usr/bin/env python3

import argparse
import os
from pathlib import Path
import re
import sys
import tomllib


OFFICIAL_REPOSITORY = "https://github.com/openai/codex.git"
REQUIRED_SOURCE_PATHS = (
    "codex-rs/core/Cargo.toml",
    "codex-rs/app-server/Cargo.toml",
    "codex-rs/app-server-protocol/Cargo.toml",
    "codex-rs/exec-server/Cargo.toml",
    "LICENSE",
    "NOTICE",
)
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate codex-upstream.lock")
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument(
        "--expected-commit",
        default=os.environ.get("RIFTX_EXPECTED_CODEX_COMMIT"),
    )
    return parser.parse_args()


def validate(root: Path, expected_commit: str | None) -> dict[str, object]:
    lock_path = root / "codex-upstream.lock"
    with lock_path.open("rb") as lock_file:
        lock = tomllib.load(lock_file)

    if lock.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    if lock.get("repository") != OFFICIAL_REPOSITORY:
        raise ValueError(f"repository must be {OFFICIAL_REPOSITORY}")

    commit = lock.get("commit")
    if not isinstance(commit, str) or COMMIT_PATTERN.fullmatch(commit) is None:
        raise ValueError("commit must be a lowercase 40-character SHA-1")
    if expected_commit is not None and commit != expected_commit:
        raise ValueError(
            f"locked commit {commit} does not match expected {expected_commit}"
        )

    excluded_paths = lock.get("excluded_paths")
    if excluded_paths != [".git", ".codex"]:
        raise ValueError('excluded_paths must be [".git", ".codex"]')
    if (root / ".codex").exists():
        raise ValueError("vendored upstream .codex directory must remain excluded")

    missing = [path for path in REQUIRED_SOURCE_PATHS if not (root / path).is_file()]
    if missing:
        raise ValueError(f"vendored Codex source is incomplete: {', '.join(missing)}")

    patched_components = lock.get("patched_components")
    if not isinstance(patched_components, list) or not all(
        isinstance(component, str) and component for component in patched_components
    ):
        raise ValueError("patched_components must be a list of non-empty paths")
    missing_components = [
        component for component in patched_components if not (root / component).exists()
    ]
    if missing_components:
        raise ValueError(
            f"patched component paths do not exist: {', '.join(missing_components)}"
        )

    return {
        "repository": lock["repository"],
        "commit": commit,
        "patched_components": patched_components,
    }


def main() -> int:
    args = parse_args()
    try:
        result = validate(args.root.resolve(), args.expected_commit)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        print(f"upstream lock validation failed: {error}", file=sys.stderr)
        return 1

    patches = result["patched_components"]
    patch_summary = ",".join(patches) if patches else "none"
    print(f"repository={result['repository']}")
    print(f"commit={result['commit']}")
    print(f"patched_components={patch_summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
