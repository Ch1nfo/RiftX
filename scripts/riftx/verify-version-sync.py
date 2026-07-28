#!/usr/bin/env python3
"""Verify VERSION is the authoritative RiftX release version."""

import json
import sys
import tomllib
from pathlib import Path


def load_versions(root: Path) -> dict[str, str]:
    expected = (root / "VERSION").read_text(encoding="utf-8").strip()
    package = json.loads(
        (root / "apps/desktop/package.json").read_text(encoding="utf-8")
    )
    tauri = json.loads(
        (root / "apps/desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8")
    )
    cargo = tomllib.loads(
        (root / "apps/desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    )
    cli = tomllib.loads(
        (root / "codex-rs/riftx-cli/Cargo.toml").read_text(encoding="utf-8")
    )
    gateway = tomllib.loads(
        (root / "codex-rs/riftx-gateway/Cargo.toml").read_text(encoding="utf-8")
    )
    return {
        "VERSION": expected,
        "apps/desktop/package.json": package["version"],
        "apps/desktop/src-tauri/tauri.conf.json": tauri["version"],
        "apps/desktop/src-tauri/Cargo.toml": cargo["package"]["version"],
        "codex-rs/riftx-cli/Cargo.toml": cli["package"]["version"],
        "codex-rs/riftx-gateway/Cargo.toml": gateway["package"]["version"],
    }


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    versions = load_versions(root)
    expected = versions["VERSION"]
    drift = {path: version for path, version in versions.items() if version != expected}
    if drift:
        for path, version in drift.items():
            print(
                f"version drift: {path}={version!r}, expected {expected!r}",
                file=sys.stderr,
            )
        return 1
    print(f"RiftX release version {expected} is synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
