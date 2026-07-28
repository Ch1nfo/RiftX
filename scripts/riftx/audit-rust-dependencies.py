#!/usr/bin/env python3
"""Run cargo-audit and enforce expiring, exact RustSec exceptions."""

import argparse
from datetime import date
import json
from pathlib import Path
import subprocess
import sys
import tomllib


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=root / "security/rustsec-exceptions.toml",
    )
    parser.add_argument("--lockfile", action="append", type=Path, default=[])
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def load_exceptions(
    path: Path, today: date
) -> dict[tuple[str, str, str], dict[str, object]]:
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    if document.get("schema_version") != 1:
        raise ValueError("RustSec exception schema_version must be 1")

    result: dict[tuple[str, str, str], dict[str, object]] = {}
    for entry in document.get("exception", []):
        advisory_id = entry.get("id")
        crate = entry.get("crate")
        versions = entry.get("versions")
        expires = entry.get("expires")
        reason = entry.get("reason")
        scope = entry.get("scope")
        if not isinstance(advisory_id, str) or not advisory_id.startswith("RUSTSEC-"):
            raise ValueError(f"invalid RustSec advisory id: {advisory_id!r}")
        if not isinstance(crate, str) or not crate:
            raise ValueError(f"exception {advisory_id} must name a crate")
        if (
            not isinstance(versions, list)
            or not versions
            or not all(isinstance(version, str) and version for version in versions)
        ):
            raise ValueError(f"exception {advisory_id} must list exact versions")
        if not isinstance(expires, str):
            raise ValueError(f"exception {advisory_id} must have an expiry date")
        expiry = date.fromisoformat(expires)
        if expiry < today:
            raise ValueError(f"exception {advisory_id} expired on {expiry.isoformat()}")
        if not isinstance(reason, str) or len(reason) < 40:
            raise ValueError(f"exception {advisory_id} needs a substantive reason")
        if not isinstance(scope, str) or not scope:
            raise ValueError(f"exception {advisory_id} must document its scope")
        if not isinstance(entry.get("release_reachable"), bool):
            raise ValueError(f"exception {advisory_id} must declare release_reachable")
        for version in versions:
            key = (advisory_id, crate, version)
            if key in result:
                raise ValueError(f"duplicate RustSec exception: {key}")
            result[key] = entry
    return result


def run_audit(lockfile: Path, fetch: bool) -> dict[str, object]:
    command = [
        "cargo",
        "audit",
        "--file",
        str(lockfile),
        "--format",
        "json",
    ]
    if not fetch:
        command.append("--no-fetch")
    process = subprocess.run(command, text=True, capture_output=True, check=False)
    try:
        report = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"cargo audit did not return JSON for {lockfile}: {process.stderr.strip()}"
        ) from error
    return report


def vulnerability_keys(report: dict[str, object]) -> set[tuple[str, str, str]]:
    vulnerabilities = report.get("vulnerabilities", {})
    entries = (
        vulnerabilities.get("list", []) if isinstance(vulnerabilities, dict) else []
    )
    result: set[tuple[str, str, str]] = set()
    for entry in entries:
        advisory = entry.get("advisory", {})
        package = entry.get("package", {})
        result.add(
            (
                advisory.get("id", ""),
                package.get("name", ""),
                package.get("version", ""),
            )
        )
    return result


def evaluate(
    reports: list[dict[str, object]],
    exceptions: dict[tuple[str, str, str], dict[str, object]],
) -> list[str]:
    found: set[tuple[str, str, str]] = set()
    for report in reports:
        found.update(vulnerability_keys(report))
    errors = [
        f"unapproved vulnerability {advisory_id} in {crate}@{version}"
        for advisory_id, crate, version in sorted(found - exceptions.keys())
    ]
    errors.extend(
        f"stale exception {advisory_id} for {crate}@{version}"
        for advisory_id, crate, version in sorted(exceptions.keys() - found)
    )
    return errors


def main() -> int:
    args = parse_args()
    try:
        exceptions = load_exceptions(args.exceptions, date.today())
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        print(f"RustSec exception validation failed: {error}", file=sys.stderr)
        return 1

    if args.validate_only:
        print(f"validated {len(exceptions)} active RustSec exception entries")
        return 0
    if not args.lockfile:
        print("at least one --lockfile is required", file=sys.stderr)
        return 2

    try:
        reports = [
            run_audit(lockfile, fetch=index == 0)
            for index, lockfile in enumerate(args.lockfile)
        ]
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    errors = evaluate(reports, exceptions)
    if errors:
        for error in errors:
            print(f"RustSec audit failed: {error}", file=sys.stderr)
        return 1
    print(
        f"RustSec audit passed for {len(args.lockfile)} lockfiles "
        f"with {len(exceptions)} active, exact exceptions"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
