#!/usr/bin/env python3
"""Reject dependencies whose reported license has no approved SPDX choice."""

import argparse
import json
from pathlib import Path
import re
import sys

ALLOWED_LICENSES = {
    "0BSD",
    "Apache-2.0",
    "BSD-1-Clause",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "BSL-1.0",
    "BlueOak-1.0.0",
    "CC-BY-4.0",
    "CC0-1.0",
    "CDLA-Permissive-2.0",
    "ISC",
    "MIT",
    "MIT-0",
    "MPL-2.0",
    "Python-2.0",
    "Unicode-3.0",
    "Unlicense",
    "Zlib",
}
ALLOWED_EXCEPTIONS = {"LLVM-exception"}
TOKEN_PATTERN = re.compile(r"\(|\)|/|\bAND\b|\bOR\b|\bWITH\b|[A-Za-z0-9.+-]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cargo-metadata",
        action="append",
        default=[],
        type=Path,
        help="cargo metadata JSON file; may be repeated",
    )
    parser.add_argument("--pnpm-licenses", type=Path)
    return parser.parse_args()


def expression_is_allowed(expression: str) -> bool:
    normalized = expression.replace("/", " OR ")
    tokens = TOKEN_PATTERN.findall(normalized)
    if "".join(tokens).replace("AND", "").replace("OR", "").replace("WITH", "") == "":
        return False
    try:
        parser = LicenseExpressionParser(tokens)
        result = parser.parse_expression()
    except ValueError:
        return False
    return result and parser.at_end()


class LicenseExpressionParser:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.index = 0

    def parse_expression(self) -> bool:
        result = self.parse_term()
        while self._consume("OR"):
            result = self.parse_term() or result
        return result

    def parse_term(self) -> bool:
        result = self.parse_factor()
        while self._consume("AND"):
            result = self.parse_factor() and result
        return result

    def parse_factor(self) -> bool:
        if self._consume("("):
            result = self.parse_expression()
            if not self._consume(")"):
                raise ValueError("missing closing parenthesis")
            return result
        license_id = self._next_identifier()
        result = license_id in ALLOWED_LICENSES
        if self._consume("WITH"):
            result = self._next_identifier() in ALLOWED_EXCEPTIONS and result
        return result

    def at_end(self) -> bool:
        return self.index == len(self.tokens)

    def _consume(self, expected: str) -> bool:
        if self.index < len(self.tokens) and self.tokens[self.index] == expected:
            self.index += 1
            return True
        return False

    def _next_identifier(self) -> str:
        if self.index >= len(self.tokens):
            raise ValueError("missing license identifier")
        token = self.tokens[self.index]
        if token in {"(", ")", "AND", "OR", "WITH", "/"}:
            raise ValueError(f"expected license identifier, found {token}")
        self.index += 1
        return token


def verify_cargo_metadata(path: Path) -> list[str]:
    report = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    for package in report.get("packages", []):
        license_expression = package.get("license")
        identity = (
            f"{package.get('name', '<unknown>')}@{package.get('version', '<unknown>')}"
        )
        if not isinstance(license_expression, str) or not expression_is_allowed(
            license_expression
        ):
            errors.append(f"Rust {identity}: {license_expression!r}")
    return errors


def verify_pnpm_licenses(path: Path) -> list[str]:
    report = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if not isinstance(report, dict):
        return ["pnpm license report is not an object"]
    for grouped_license, packages in report.items():
        for package in packages:
            license_expression = package.get("license", grouped_license)
            versions = ",".join(package.get("versions", []))
            identity = f"{package.get('name', '<unknown>')}@{versions or '<unknown>'}"
            if not isinstance(license_expression, str) or not expression_is_allowed(
                license_expression
            ):
                errors.append(f"Node {identity}: {license_expression!r}")
    return errors


def main() -> int:
    args = parse_args()
    if not args.cargo_metadata and args.pnpm_licenses is None:
        print("at least one license report is required", file=sys.stderr)
        return 2

    errors: list[str] = []
    for path in args.cargo_metadata:
        errors.extend(verify_cargo_metadata(path))
    if args.pnpm_licenses is not None:
        errors.extend(verify_pnpm_licenses(args.pnpm_licenses))

    if errors:
        for error in errors:
            print(f"unapproved or missing dependency license: {error}", file=sys.stderr)
        return 1
    print("dependency license reports satisfy the RiftX release allowlist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
