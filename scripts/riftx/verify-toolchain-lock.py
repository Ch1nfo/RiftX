#!/usr/bin/env python3
"""Verify the release toolchain and Tauri dependency pins."""

import json
from pathlib import Path
import re
import sys
import tomllib

RUST_VERSION = "1.95.0"
NODE_VERSION = "22.20.0"
PNPM_VERSION = "10.33.0"
TAURI_JS_VERSIONS = {
    "@tauri-apps/api": "2.11.1",
    "@tauri-apps/cli": "2.11.4",
}
TAURI_RUST_VERSIONS = {
    "tauri-build": "2.6.3",
    "tauri": "2.11.5",
    "tauri-plugin-notification": "2.3.3",
    "tauri-plugin-shell": "2.3.5",
}


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    rust_toolchain = tomllib.loads(
        (root / "codex-rs/rust-toolchain.toml").read_text(encoding="utf-8")
    )
    _expect(
        errors,
        "codex-rs/rust-toolchain.toml toolchain.channel",
        rust_toolchain["toolchain"]["channel"],
        RUST_VERSION,
    )

    environment_text = (root / ".github/agent-environment.yml").read_text(
        encoding="utf-8"
    )
    rust_match = re.search(r"^\s*-\s*rust=([^\s#]+)", environment_text, re.MULTILINE)
    _expect(
        errors,
        ".github/agent-environment.yml rust",
        rust_match.group(1) if rust_match else None,
        RUST_VERSION,
    )

    _expect(
        errors,
        ".node-version",
        (root / ".node-version").read_text(encoding="utf-8").strip(),
        NODE_VERSION,
    )

    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    _expect(
        errors, "package.json engines.node", package["engines"]["node"], NODE_VERSION
    )
    _expect(
        errors, "package.json engines.pnpm", package["engines"]["pnpm"], PNPM_VERSION
    )
    package_manager = package.get("packageManager", "")
    if not package_manager.startswith(f"pnpm@{PNPM_VERSION}+sha512."):
        errors.append(
            "package.json packageManager must pin pnpm "
            f"{PNPM_VERSION} with an integrity hash, found {package_manager!r}"
        )

    desktop = json.loads(
        (root / "apps/desktop/package.json").read_text(encoding="utf-8")
    )
    for dependency, expected in TAURI_JS_VERSIONS.items():
        section = (
            "dependencies" if dependency == "@tauri-apps/api" else "devDependencies"
        )
        _expect(
            errors,
            f"apps/desktop/package.json {section}.{dependency}",
            desktop[section].get(dependency),
            expected,
        )

    cargo = tomllib.loads(
        (root / "apps/desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    )
    for dependency, expected in TAURI_RUST_VERSIONS.items():
        section = (
            "build-dependencies" if dependency == "tauri-build" else "dependencies"
        )
        actual = cargo[section].get(dependency)
        if isinstance(actual, dict):
            actual = actual.get("version")
        _expect(
            errors,
            f"apps/desktop/src-tauri/Cargo.toml {section}.{dependency}",
            actual,
            f"={expected}",
        )

    for workflow_path in (
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
    ):
        workflow = (root / workflow_path).read_text(encoding="utf-8")
        if "node-version: 22\n" in workflow:
            errors.append(f"{workflow_path} must use the exact Node version pin")
        if f"node-version: {NODE_VERSION}" not in workflow:
            errors.append(f"{workflow_path} does not reference Node {NODE_VERSION}")
        if f"version: {PNPM_VERSION}" not in workflow:
            errors.append(f"{workflow_path} does not reference pnpm {PNPM_VERSION}")

    return errors


def _expect(errors: list[str], label: str, actual: object, expected: object) -> None:
    if actual != expected:
        errors.append(f"{label}={actual!r}, expected {expected!r}")


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    errors = validate(root)
    if errors:
        for error in errors:
            print(f"toolchain pin validation failed: {error}", file=sys.stderr)
        return 1
    print(
        "release toolchain pins verified: "
        f"rust={RUST_VERSION} node={NODE_VERSION} pnpm={PNPM_VERSION}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
